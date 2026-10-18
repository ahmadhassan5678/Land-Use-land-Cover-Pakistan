from __future__ import annotations
import os, json, math, hashlib, logging, zipfile, concurrent.futures
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

# Windows PROJ database compatibility fix. PostgreSQL/PostGIS can set PROJ_LIB
# or PROJ_DATA to an older proj.db that conflicts with the versions bundled
# with Rasterio/PyProj. Clear only those known conflicting overrides before
# importing GIS libraries so they can use their own compatible data directories.
for _proj_env in ("PROJ_LIB", "PROJ_DATA"):
    _proj_path = os.environ.get(_proj_env, "")
    _proj_lower = _proj_path.lower().replace("/", "\\")
    if "postgresql" in _proj_lower or "postgis" in _proj_lower:
        os.environ.pop(_proj_env, None)

import requests
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.io import MemoryFile
from rasterio.mask import mask
from rasterio.warp import calculate_default_transform, reproject, Resampling
from PIL import Image
from rasterio.features import geometry_mask
from shapely.geometry import mapping, box
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from pyproj import CRS

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / 'uploads'
DATA = BASE / 'data'
OUTPUTS = BASE / 'outputs'
for folder in (UPLOADS, DATA, OUTPUTS): folder.mkdir(parents=True, exist_ok=True)
load_dotenv(BASE / '.env')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_UPLOAD_MB', '100')) * 1024 * 1024
app.config['JSON_SORT_KEYS'] = False
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Configure a verified Esri ImageServer endpoint here if the default endpoint changes.
ESRI_LANDCOVER_URL = os.getenv('ESRI_LANDCOVER_IMAGE_SERVER_URL', 'https://ic.imagery1.arcgis.com/arcgis/rest/services/Sentinel2_10m_LandCover/ImageServer')
# Copernicus DEM downloads may require an authenticated provider. Set OPENTOPOGRAPHY_API_KEY
# to enable the OpenTopography global DEM endpoint; the app never silently substitutes sample data.
OPENTOPOGRAPHY_API_KEY = os.getenv('OPENTOPOGRAPHY_API_KEY', '').strip()
GADM_URL = os.getenv('GADM_PAK_GPKG_URL', 'https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg/gadm41_PAK.gpkg')

CLASS_INFO = {
    1: ('Water', '#1976D2'),
    2: ('Vegetation', '#228B22'),
    4: ('Vegetation / wetland', '#3E8E41'),
    5: ('Cropland', '#F2C811'),
    7: ('Built-up', '#E53935'),
    8: ('Barren land', '#D3D3D3'),
    9: ('Snow / ice', '#FFFFFF'),
    10: ('Cloud / no-data', '#9E9E9E'),
    11: ('Rangeland', '#A8C66C'),
}

class UserError(Exception): pass

def safe_error(exc):
    logging.exception('Processing failure')
    return str(exc)[:700] or exc.__class__.__name__

def validate_sidecars(files):
    # Only a shapefile bundle is accepted; no archive or alternative vector format.
    allowed = {'.shp', '.shx', '.dbf', '.prj'}
    supplied = {}
    supplied_count = 0
    for f in files:
        if not f or not f.filename: continue
        supplied_count += 1
        name = secure_filename(f.filename)
        ext = Path(name).suffix.lower()
        if ext not in allowed:
            raise UserError(f'Unsupported file: {name}. Upload only .shp, .shx, .dbf and .prj files.')
        stem = Path(name).stem.lower()
        supplied.setdefault(stem, {})[ext] = f
    if supplied_count != 4:
        raise UserError('Upload exactly four files: one each of .shp, .shx, .dbf and .prj.')
    if len(supplied) != 1:
        raise UserError('Upload the four sidecar files for one shapefile, using the same filename stem.')
    stem, sidecars = next(iter(supplied.items()))
    if len(sidecars) != 4:
        raise UserError('Upload exactly four files: one each of .shp, .shx, .dbf and .prj.')
    missing = allowed - set(sidecars)
    if missing:
        raise UserError('Missing required shapefile sidecar(s): ' + ', '.join(sorted(missing)))
    return stem, sidecars

def choose_utm(gdf):
    geom = gdf.geometry.union_all() if hasattr(gdf.geometry, 'union_all') else gdf.unary_union
    lon, lat = geom.representative_point().x, geom.representative_point().y
    zone = utm_zone_for_lon(lon)
    epsg = (32600 if lat >= 0 else 32700) + zone
    return zone, CRS.from_epsg(epsg)

def utm_zone_for_lon(lon):
    return max(1, min(60, int(math.floor((float(lon) + 180.0) / 6.0) + 1)))

def utm_zone_label(zone, lat=25):
    return f"UTM {int(zone)}{'N' if lat >= 0 else 'S'}"

def zone_parts_wgs84(gdf):
    """Split polygon features by standard 6-degree UTM longitude bands."""
    from shapely.geometry import box
    out=[]
    wgs=gdf.to_crs('EPSG:4326') if gdf.crs is None or str(gdf.crs).upper() not in ('EPSG:4326','OGC:CRS84') else gdf.copy()
    for feature_idx, row in wgs.iterrows():
        geom=row.geometry
        if geom is None or geom.is_empty: continue
        minx,miny,maxx,maxy=geom.bounds
        first=utm_zone_for_lon(minx); last=utm_zone_for_lon(maxx if maxx < 180 else 179.999999)
        for zone in range(first,last+1):
            west=-180+(zone-1)*6; east=west+6
            part=geom.intersection(box(west,-90,east,90))
            if part.is_empty or part.area <= 0: continue
            out.append({'feature_index':str(feature_idx),'zone':zone,'label':utm_zone_label(zone,(miny+maxy)/2), 'geometry':part})
    return out

def boundary_info(gdf):
    if gdf.crs is None: raise UserError('The uploaded boundary has no readable CRS. Include a valid .prj file.')
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    if gdf.empty: raise UserError('Boundary has no usable geometry.')
    if not gdf.geometry.geom_type.isin(['Polygon', 'MultiPolygon']).all():
        raise UserError('Only polygon or multipolygon shapefiles are accepted as study boundaries.')
    gdf = gdf.to_crs('EPSG:4326')
    if not gdf.geometry.is_valid.all():
        gdf['geometry'] = gdf.geometry.make_valid()
    # Avoid near-global or obviously incorrect coordinates after reprojection.
    bounds = gdf.total_bounds
    if bounds[0] < -180 or bounds[2] > 180 or bounds[1] < -90 or bounds[3] > 90:
        raise UserError('Boundary coordinates are invalid after conversion to WGS84.')
    zone, utm = choose_utm(gdf)
    projected = gdf.to_crs(utm)
    area_m2 = float(projected.geometry.area.sum())
    if area_m2 <= 0: raise UserError('Study-area polygon has zero area.')
    return gdf, projected, zone, utm, area_m2 / 1_000_000

def cache_key(gdf):
    bounds = ','.join(f'{x:.5f}' for x in gdf.total_bounds)
    return hashlib.sha256((bounds + ESRI_LANDCOVER_URL).encode()).hexdigest()[:18]

def download_esri_landcover(gdf, out_path):
    """Export Esri's classified land-cover raster and cache it locally."""
    from rasterio.warp import transform_bounds

    bounds_wgs84 = tuple(map(float, gdf.total_bounds))
    minx, miny, maxx, maxy = transform_bounds(
        "EPSG:4326", "EPSG:3857", *bounds_wgs84, densify_pts=21
    )
    width_m = max(1.0, maxx - minx)
    height_m = max(1.0, maxy - miny)

    # Esri ImageServer metadata advertises maxImageWidth/Height=4000.
    scale = min(
        1.0,
        4000.0 * 10.0 / max(width_m, height_m),
        math.sqrt((15_000_000.0 * 100.0) / (width_m * height_m)),
    )
    width = max(1, min(4000, int(math.ceil(width_m * scale / 10.0))))
    height = max(1, min(4000, int(math.ceil(height_m * scale / 10.0))))

    key = cache_key(gdf)
    cached = DATA / f'esri_landcover_{key}.tif'
    if cached.exists() and cached.stat().st_size > 1024:
        import shutil
        shutil.copy2(cached, out_path)
        return {'source': ESRI_LANDCOVER_URL, 'cache': 'hit',
                'export_size': [width, height], 'export_crs': 'EPSG:3857'}

    export_url = ESRI_LANDCOVER_URL.rstrip('/') + '/exportImage'
    params = {
        'bbox': f'{minx:.6f},{miny:.6f},{maxx:.6f},{maxy:.6f}',
        'bboxSR': 3857, 'imageSR': 3857, 'size': f'{width},{height}',
        'format': 'tiff', 'pixelType': 'U8',
        'interpolation': 'RSP_NearestNeighbor', 'noData': 0,
        'noDataInterpretation': 'esriNoDataMatchAny', 'f': 'json',
    }

    def error_message(payload):
        if isinstance(payload, dict) and isinstance(payload.get('error'), dict):
            err = payload['error']
            details = '; '.join(str(x) for x in err.get('details', []) if x)
            msg = str(err.get('message') or 'Unknown Esri ImageServer error')
            return f'{msg}: {details}' if details else msg
        return None

    try:
        response = requests.get(export_url, params=params, timeout=(20, 180))
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            err = error_message(payload)
            if err:
                raise UserError(f'Esri Land Cover export failed: {err}')
            href = payload.get('href')
            if href:
                image_response = requests.get(href, timeout=(20, 180))
                image_response.raise_for_status()
                raw = image_response.content
                content_type = image_response.headers.get('Content-Type', '').lower()
            else:
                # Retry in image mode for servers that do not return an href in JSON mode.
                image_params = dict(params)
                image_params['f'] = 'image'
                image_response = requests.get(export_url, params=image_params, timeout=(20, 180))
                image_response.raise_for_status()
                raw = image_response.content
                content_type = image_response.headers.get('Content-Type', '').lower()
                try:
                    image_payload = image_response.json()
                except ValueError:
                    image_payload = None
                err = error_message(image_payload)
                if err:
                    raise UserError(f'Esri Land Cover export failed: {err}')
        else:
            raw = response.content
            content_type = response.headers.get('Content-Type', '').lower()

        if len(raw) < 1024 or b'<html' in raw[:300].lower():
            excerpt = raw[:300].decode('utf-8', errors='replace').strip()
            raise UserError('Esri export response was not a usable raster. '
                            f'Content-Type={content_type or "unknown"}; response={excerpt[:180]}')

        with open(out_path, 'wb') as f:
            f.write(raw)
        try:
            with rasterio.open(out_path) as ds:
                if ds.count < 1 or ds.width < 1 or ds.height < 1:
                    raise UserError('Esri returned an empty raster.')
                if ds.crs is None:
                    raise UserError('Esri returned a raster without a coordinate reference system.')
        except UserError:
            raise
        except Exception as e:
            try:
                with open(out_path, 'rb') as f:
                    head = f.read(500).decode('utf-8', errors='replace')
            except Exception:
                head = ''
            raise UserError('Esri returned a response that Rasterio cannot open as a GeoTIFF. '
                            f'Content-Type={content_type or "unknown"}; detail={e}; response={head[:180]}')

        import shutil
        shutil.copy2(out_path, cached)
        return {'source': ESRI_LANDCOVER_URL, 'cache': 'miss; downloaded and cached',
                'export_size': [width, height], 'export_crs': 'EPSG:3857',
                'content_type': content_type}
    except UserError:
        raise
    except requests.RequestException as e:
        raise UserError(f'Esri land-cover download failed: {e}. Check internet access and '
                        'ESRI_LANDCOVER_IMAGE_SERVER_URL in .env/config.')

def reproject_raster(src_path, dst_path, target_crs):
    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(src.crs, target_crs, src.width, src.height, *src.bounds)
        if width * height > 25_000_000:
            factor = math.sqrt((width * height) / 25_000_000)
            width, height = max(1, int(width/factor)), max(1, int(height/factor))
            transform, width, height = calculate_default_transform(src.crs, target_crs, src.width, src.height, *src.bounds, dst_width=width, dst_height=height)
        profile = src.profile.copy(); profile.update(crs=target_crs, transform=transform, width=width, height=height, compress='deflate')
        with rasterio.open(dst_path, 'w', **profile) as dst:
            for i in range(1, src.count+1):
                reproject(source=rasterio.band(src, i), destination=rasterio.band(dst, i), src_transform=src.transform, src_crs=src.crs, dst_transform=transform, dst_crs=target_crs, resampling=Resampling.nearest, src_nodata=src.nodata, dst_nodata=src.nodata)

def clip_and_summarize(landcover_path, boundary_projected, utm, output_dir):
    projected_path = output_dir / 'landcover_projected.tif'
    reproject_raster(landcover_path, projected_path, utm)
    with rasterio.open(projected_path) as src:
        geoms = [mapping(g) for g in boundary_projected.geometry]
        arr, transform = mask(src, geoms, crop=True, filled=False)
        data = arr[0]
        valid = ~np.ma.getmaskarray(data)
        if src.nodata is not None: valid &= (data.data != src.nodata)
        values = data.data
        # ESRI land-cover codes are expected; do not count cloud/no-data as a land-cover class.
        valid &= (values != 0) & (values != 10)
        pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
        if pixel_area <= 0: raise UserError('Could not determine valid raster pixel area.')
        codes, counts = np.unique(values[valid].astype('int64'), return_counts=True)
        total_pixels = int(counts.sum())
        if total_pixels == 0: raise UserError('No valid land-cover pixels intersect the study area. Check the Esri service coverage and uploaded CRS.')
        rows = []
        for code, count in zip(codes, counts):
            name, color = CLASS_INFO.get(int(code), (f'Other / unclassified ({int(code)})', '#808080'))
            area = float(count * pixel_area / 1_000_000)
            rows.append({'code': int(code), 'class': name, 'color': color, 'pixels': int(count), 'area_km2': round(area, 6)})
        total_area = sum(r['area_km2'] for r in rows)
        for r in rows: r['percentage'] = round((r['area_km2'] / total_area * 100), 4) if total_area else 0
        # Preserve source codes and use 0 for pixels outside polygon / invalid data.
        out_arr = np.where(valid, values, 0).astype('uint8')
        profile = src.profile.copy(); profile.update(driver='GTiff', height=out_arr.shape[0], width=out_arr.shape[1], transform=transform, count=1, dtype='uint8', nodata=0, compress='deflate')
        clipped = output_dir / 'landcover_clipped.tif'
        with rasterio.open(clipped, 'w', **profile) as dst: dst.write(out_arr, 1)
    return rows, total_area, clipped, pixel_area


def create_landcover_preview(clipped_path, output_dir):
    """Create transparent RGBA overlay in WGS84 for the web map and 3D terrain drape."""
    with rasterio.open(clipped_path) as src:
        dst_transform, dst_width, dst_height = calculate_default_transform(src.crs, 'EPSG:4326', src.width, src.height, *src.bounds)
        factor = max(1.0, math.sqrt((dst_width * dst_height) / 4_000_000))
        if factor > 1:
            dst_width, dst_height = max(1, int(dst_width/factor)), max(1, int(dst_height/factor))
            dst_transform, dst_width, dst_height = calculate_default_transform(src.crs, 'EPSG:4326', src.width, src.height, *src.bounds, dst_width=dst_width, dst_height=dst_height)
        target = np.zeros((dst_height, dst_width), dtype=np.uint8)
        reproject(source=rasterio.band(src, 1), destination=target, src_transform=src.transform, src_crs=src.crs, dst_transform=dst_transform, dst_crs='EPSG:4326', resampling=Resampling.nearest, src_nodata=0, dst_nodata=0)
        rgba = np.zeros((dst_height, dst_width, 4), dtype=np.uint8)
        for code, (_name, hex_color) in CLASS_INFO.items():
            if code in (10,): continue
            rgb = tuple(int(hex_color[i:i+2],16) for i in (1,3,5))
            mask_arr = target == code
            rgba[mask_arr,0] = rgb[0]; rgba[mask_arr,1] = rgb[1]; rgba[mask_arr,2] = rgb[2]; rgba[mask_arr,3] = 210
        # Unknown source codes remain gray but visible; zero remains transparent.
        unknown = (target != 0) & ~np.isin(target, list(CLASS_INFO.keys()))
        rgba[unknown] = (128,128,128,210)
        preview = output_dir / 'landcover_preview.png'
        Image.fromarray(rgba, mode='RGBA').save(preview, optimize=True)
        west = dst_transform.c
        north = dst_transform.f
        east = west + dst_transform.a * dst_width + dst_transform.b * dst_height
        south = north + dst_transform.d * dst_width + dst_transform.e * dst_height
        bounds = [float(west), float(south), float(east), float(north)]
    return preview, bounds

def _copernicus_tile_url(lat, lon, resolution='90m'):
    # Public Copernicus DEM COG archive naming: 10 = GLO-30, 30 = GLO-90.
    product = '10' if resolution == '30m' else '30'
    ns = 'N' if lat >= 0 else 'S'; ew = 'E' if lon >= 0 else 'W'
    lat_txt=f'{abs(int(lat)):02d}'; lon_txt=f'{abs(int(lon)):03d}'
    folder=f'Copernicus_DSM_COG_{product}_{ns}{lat_txt}_00_{ew}{lon_txt}_00_DEM'
    return f'https://copernicus-dem-{resolution.replace("m", "m")}.s3.amazonaws.com/{folder}/{folder}.tif'

def fetch_dem(boundary_wgs84, boundary_projected, utm, output_dir):
    """Fetch COP30 from OpenTopography with a key, otherwise public COP90 COG tiles.

    Public-tile fallback is capped to 300 one-degree tiles to prevent an accidental
    enormous download for continental-scale polygons. All successful tiles are cached.
    """
    target = output_dir / 'dem_source.tif'
    clipped = output_dir / 'dem_clipped.tif'
    projected = output_dir / 'dem_utm.tif'
    cache_key_value=cache_key(boundary_wgs84)
    cached=DATA/f'dem_clipped_{cache_key_value}.tif'
    try:
        if cached.exists() and cached.stat().st_size > 1024:
            import shutil
            shutil.copy2(cached, clipped)
            reproject_raster(clipped, projected, utm)
            source_label='Cached Copernicus DEM GLO-90 public tiles'
        elif OPENTOPOGRAPHY_API_KEY:
            b=boundary_wgs84.total_bounds
            params={'demtype':'COP30','south':float(b[1]),'north':float(b[3]),'west':float(b[0]),'east':float(b[2]),'outputFormat':'GTiff','API_Key':OPENTOPOGRAPHY_API_KEY}
            r=requests.get('https://portal.opentopography.org/API/globaldem',params=params,timeout=(20,300)); r.raise_for_status()
            if len(r.content)<1024 or r.content[:4] not in (b'II*\x00',b'MM\x00*'):
                raise ValueError('OpenTopography returned an error response rather than a GeoTIFF. Check API key, request limits, and provider message.')
            target.write_bytes(r.content)
            with rasterio.open(target) as src:
                if src.crs is None: raise ValueError('Downloaded DEM has no CRS.')
                geoms=[mapping(g) for g in boundary_wgs84.to_crs(src.crs).geometry]
                fill=src.nodata if src.nodata is not None else -9999
                arr,trans=mask(src,geoms,crop=True,filled=True,nodata=fill)
                profile=src.profile.copy(); profile.update(height=arr.shape[1],width=arr.shape[2],transform=trans,nodata=fill,compress='deflate')
                with rasterio.open(clipped,'w',**profile) as dst: dst.write(arr)
            import shutil; shutil.copy2(clipped,cached)
            reproject_raster(clipped,projected,utm)
            source_label='OpenTopography Global DEM API (COP30)'
        else:
            b=boundary_wgs84.total_bounds
            west,south,east,north=map(float,b)
            min_lon=max(-180,math.floor(west)); max_lon=min(179,math.ceil(east)-1)
            min_lat=max(-89,math.floor(south)); max_lat=min(89,math.ceil(north)-1)
            tile_list=[(lat,lon) for lat in range(min_lat,max_lat+1) for lon in range(min_lon,max_lon+1)]
            if len(tile_list)>300:
                return {'status':'not_downloaded','message':f'DEM not downloaded: boundary spans {len(tile_list)} one-degree tiles, above the 300-tile safety limit. Set OPENTOPOGRAPHY_API_KEY in .env for a bounded COP30 API request or use a smaller study area.'},None
            tile_dir=DATA/'copernicus_dem90_tiles'; tile_dir.mkdir(parents=True,exist_ok=True)
            def get_tile(pair):
                lat,lon=pair
                url=_copernicus_tile_url(lat,lon,'90m')
                # Tile names are cached by integer degree; try the documented public COG path.
                folder=url.rsplit('/',1)[0].rsplit('/',1)[-1]
                local=tile_dir/f'{folder}.tif'
                if local.exists() and local.stat().st_size>1024: return str(local),None
                try:
                    rr=requests.get(url,timeout=(15,120)); rr.raise_for_status()
                    if len(rr.content)<1024 or rr.content[:4] not in (b'II*\x00',b'MM\x00*'):
                        return None,f'{folder}: server did not return a GeoTIFF'
                    local.write_bytes(rr.content)
                    with rasterio.open(local) as test:
                        if test.crs is None: raise ValueError('missing CRS')
                    return str(local),None
                except Exception as ex:
                    return None,f'{folder}: {ex}'
            paths=[]; failures=[]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                for path,err in pool.map(get_tile,tile_list):
                    if path: paths.append(path)
                    if err: failures.append(err)
            if not paths:
                detail='; '.join(failures[:3])
                return {'status':'error','message':'Automatic public Copernicus GLO-90 tile download failed. '+detail+' Configure OPENTOPOGRAPHY_API_KEY in .env and retry.'},None
            from rasterio.merge import merge
            datasets=[]
            try:
                datasets=[rasterio.open(x) for x in paths]
                mosaic,trans=merge(datasets)
                profile=datasets[0].profile.copy(); profile.update(driver='GTiff',height=mosaic.shape[1],width=mosaic.shape[2],transform=trans,compress='deflate',tiled=True)
                with rasterio.open(target,'w',**profile) as dst: dst.write(mosaic)
            finally:
                for ds in datasets: ds.close()
            with rasterio.open(target) as src:
                geoms=[mapping(g) for g in boundary_wgs84.to_crs(src.crs).geometry]
                fill=src.nodata if src.nodata is not None else -9999
                arr,trans=mask(src,geoms,crop=True,filled=True,nodata=fill)
                profile=src.profile.copy(); profile.update(height=arr.shape[1],width=arr.shape[2],transform=trans,nodata=fill,compress='deflate')
                with rasterio.open(clipped,'w',**profile) as dst: dst.write(arr)
            import shutil; shutil.copy2(clipped,cached)
            reproject_raster(clipped,projected,utm)
            source_label='Copernicus DEM GLO-90 public COG tiles (90 m fallback)'
        with rasterio.open(projected) as ds:
            a=ds.read(1,masked=True).astype('float64')
            if a.count()==0: raise ValueError('DEM clip contains no valid elevation pixels.')
            stats={'min_m':round(float(a.min()),2),'max_m':round(float(a.max()),2),'mean_m':round(float(a.mean()),2),'resolution_m':round(max(abs(ds.res[0]),abs(ds.res[1])),2)}
        return {'status':'success','source':source_label,'stats':stats,'note':'Elevation model is a surface model where source coverage is DSM; DEM tile resolution and source are included in this result. 3D map terrain tiles are an online visualization source.'},projected
    except Exception as e:
        return {'status':'error','message':f'DEM download/clip failed: {e}. Check internet access, provider limits, and .env API key if configured.'},None


def summarize_district_layer(districts, boundary_wgs84, output_dir, landcover_path, utm, csv_name, source_label):
    """Summarize any supplied administrative polygons against valid classified pixels."""
    study = boundary_wgs84.to_crs(utm).geometry.union_all()
    districts = districts.copy()
    if districts.crs is None:
        raise UserError(f'{source_label} has no readable coordinate reference system.')
    districts = districts.to_crs(utm)
    districts = districts[districts.geometry.notna() & ~districts.geometry.is_empty]
    districts = districts[districts.intersects(study)].copy()
    if districts.empty:
        return {'status':'success','source':source_label,'district_count':0,'message':'No supplied administrative polygons intersect the study area.'}, []
    districts['geometry'] = districts.geometry.intersection(study)
    districts = districts[~districts.geometry.is_empty]
    records=[]
    with rasterio.open(landcover_path) as ds:
        raster = ds.read(1)
        pixel_area = abs(ds.res[0] * ds.res[1])
        names = [c for c in districts.columns if c.lower() in ('name_2','name_1','name','district','district_name','adm2_en','shape_name','tehsil')]
        namecol = names[0] if names else None
        for idx, row in districts.iterrows():
            geom = row.geometry
            inside = geometry_mask([mapping(geom)], out_shape=raster.shape, transform=ds.transform, invert=True)
            vals = raster[inside & (raster != 0) & (raster != 10)]
            district_name=str(row[namecol]) if namecol else f'Area {len(records)+1}'
            # Calculate which UTM zones each clipped polygon intersects and the exact projected area per zone.
            geom_wgs=gpd.GeoSeries([geom],crs=utm).to_crs('EPSG:4326').iloc[0]
            zparts=zone_parts_wgs84(gpd.GeoDataFrame(geometry=[geom_wgs],crs='EPSG:4326'))
            zone_area_fields={}
            for zp in zparts:
                zcrs=CRS.from_epsg((32600 if zp['geometry'].representative_point().y>=0 else 32700)+zp['zone'])
                za=float(gpd.GeoSeries([zp['geometry']],crs='EPSG:4326').to_crs(zcrs).area.iloc[0]/1e6)
                zone_area_fields[f"{zp['label']}_area_km2"]=round(za,6)
                zone_area_fields[f"{zp['label']}_pct_of_polygon"]=round(za/(float(geom.area/1e6))*100,4) if geom.area else 0
            rec={'district':district_name, 'area_km2':round(float(geom.area/1e6),6), 'classified_area_km2':round(float(len(vals)*pixel_area/1e6),6), 'utm_zones':', '.join(sorted({zp['label'] for zp in zparts})), **zone_area_fields}
            total = len(vals) * pixel_area / 1e6
            for code in np.unique(vals):
                if int(code) == 10: continue
                info=CLASS_INFO.get(int(code),(f'Other / unclassified ({int(code)})','#808080'))
                area=float(np.sum(vals==code)*pixel_area/1e6)
                rec[info[0]+'_km2']=round(area,6)
                rec[info[0]+'_pct']=round(area/total*100,4) if total else 0
            records.append(rec)
    pd.DataFrame(records).to_csv(output_dir/csv_name,index=False)
    return {'status':'success','source':source_label,'district_count':len(records)}, records

def boundary_zone_summary(boundary_wgs84, output_dir):
    """For every polygon feature, report its area fragment in each intersecting UTM zone."""
    records=[]
    wgs=boundary_wgs84.to_crs('EPSG:4326')
    name_candidates=[c for c in wgs.columns if c.lower() in ('name','name_1','name_2','district','district_name','adm2_en','shape_name','id','name_3')]
    namecol=name_candidates[0] if name_candidates else None
    parts=zone_parts_wgs84(wgs)
    totals={}
    for part in parts:
        totals[part['feature_index']]=totals.get(part['feature_index'],0.0)+float(gpd.GeoSeries([part['geometry']],crs='EPSG:4326').to_crs(CRS.from_epsg((32600 if part['geometry'].representative_point().y>=0 else 32700)+part['zone'])).area.iloc[0]/1e6)
    for part in parts:
        idx=part['feature_index']
        try: row=wgs.loc[int(idx)]
        except Exception: row=None
        name=str(row[namecol]) if row is not None and namecol and namecol in row else f'Polygon {idx}'
        lat=float(part['geometry'].representative_point().y)
        crs=CRS.from_epsg((32600 if lat>=0 else 32700)+part['zone'])
        area=float(gpd.GeoSeries([part['geometry']],crs='EPSG:4326').to_crs(crs).area.iloc[0]/1e6)
        records.append({'polygon_id':idx,'polygon_name':name,'utm_zone':part['label'],'zone_number':part['zone'],'epsg':crs.to_epsg(),'area_in_zone_km2':round(area,6),'percentage_of_polygon_area':round(area/totals[idx]*100,4) if totals.get(idx) else 0})
    if records: pd.DataFrame(records).to_csv(output_dir/'boundary_polygon_utm_zones.csv',index=False)
    return {'status':'success','polygon_count':int(wgs.shape[0]),'zone_fragments':len(records),'csv':'boundary_polygon_utm_zones.csv'},records

def utm_zone_analysis(boundary_wgs84, landcover_source_path, output_dir):
    """Create per-zone boundary area and land-cover area/percent statistics.

    Each zone fragment is measured in its own WGS84 / UTM zone CRS. Source pixels
    are reprojected with nearest-neighbour resampling per zone before counting.
    """
    records=[]; zone_geoms=zone_parts_wgs84(boundary_wgs84)
    if not zone_geoms: return {'status':'error','message':'No UTM zone fragments could be derived.'}, []
    for zone in sorted(set(x['zone'] for x in zone_geoms)):
        fragments=[x['geometry'] for x in zone_geoms if x['zone']==zone]
        from shapely.geometry import GeometryCollection
        from shapely.ops import unary_union
        part=unary_union(fragments)
        lat=float(part.representative_point().y)
        epsg=(32600 if lat>=0 else 32700)+zone
        crs=CRS.from_epsg(epsg)
        area=float(gpd.GeoSeries([part],crs='EPSG:4326').to_crs(crs).area.iloc[0]/1e6)
        zone_path=output_dir/f'zone_{zone}_landcover.tif'
        reproject_raster(landcover_source_path,zone_path,crs)
        with rasterio.open(zone_path) as ds:
            geom_proj=gpd.GeoSeries([part],crs='EPSG:4326').to_crs(crs).iloc[0]
            arr,trans=mask(ds,[mapping(geom_proj)],crop=True,filled=False)
            data=arr[0]; valid=~np.ma.getmaskarray(data)
            vals=data.data
            if ds.nodata is not None: valid &= vals!=ds.nodata
            valid &= (vals!=0)&(vals!=10)
            pixel_area=abs(trans.a*trans.e-trans.b*trans.d)/1e6
            codes,counts=np.unique(vals[valid].astype('int64'),return_counts=True)
            classified=float(counts.sum()*pixel_area)
            for code,count in zip(codes,counts):
                name,color=CLASS_INFO.get(int(code),(f'Other / unclassified ({int(code)})','#808080'))
                area_cls=float(count*pixel_area)
                records.append({'utm_zone':utm_zone_label(zone,lat),'zone_number':zone,'epsg':epsg,'class':name,'color':color,'zone_polygon_area_km2':round(area,6),'classified_area_km2':round(classified,6),'area_km2':round(area_cls,6),'percentage_of_valid_classified_area':round(area_cls/classified*100,4) if classified else 0,'percentage_of_zone_polygon':round(area_cls/area*100,4) if area else 0})
        try: zone_path.unlink()
        except Exception: pass
    pd.DataFrame(records).to_csv(output_dir/'utm_zone_landcover_summary.csv',index=False)
    return {'status':'success','zone_count':len(set(x['zone'] for x in zone_geoms)),'zones':sorted(set(x['zone'] for x in zone_geoms)),'csv':'utm_zone_landcover_summary.csv','note':'Each zone fragment is measured in its corresponding UTM CRS; percentages use valid classified pixels within that zone.'},records

def district_summary(boundary_wgs84, output_dir, landcover_path, utm, uploaded_districts=None):
    """Calculate both downloaded Pakistan admin-level-2 and user-uploaded polygon summaries."""
    results = {'pakistan_admin': {'status':'unavailable','message':'Pakistan administrative boundary source not yet processed.'},
               'uploaded_boundaries': {'status':'not_provided','message':'No optional district/administrative boundary bundle was uploaded.'}}
    rows = {'pakistan_admin': [], 'uploaded_boundaries': []}
    gpkg = DATA / 'gadm41_PAK.gpkg'
    if not gpkg.exists():
        try:
            r = requests.get(GADM_URL, timeout=(20,240)); r.raise_for_status()
            if len(r.content) < 100000: raise ValueError('download is unexpectedly small')
            gpkg.write_bytes(r.content)
        except Exception as e:
            results['pakistan_admin']={'status':'unavailable','message':f'Pakistan administrative boundaries could not be downloaded: {e}. This source is non-official; upload district polygons for an additional analysis.'}
    if gpkg.exists():
        try:
            layers = gpd.list_layers(gpkg).name.tolist()
            layer = next((x for x in layers if x.upper().endswith('ADM_2')), None) or next((x for x in layers if x.endswith('_2')), None)
            if not layer: raise ValueError('GADM GeoPackage does not contain an admin level 2 layer.')
            districts = gpd.read_file(gpkg, layer=layer)
            if districts.crs is None: districts = districts.set_crs('EPSG:4326')
            results['pakistan_admin'], rows['pakistan_admin'] = summarize_district_layer(
                districts, boundary_wgs84, output_dir, landcover_path, utm,
                'pakistan_district_landcover_summary.csv', 'GADM 4.1 Pakistan administrative level 2 (non-official)')
            if results['pakistan_admin'].get('status') == 'success':
                results['pakistan_admin']['note']='GADM is not an official Government of Pakistan boundary authority. Verify administrative boundaries before official reporting.'
        except Exception as e:
            results['pakistan_admin']={'status':'error','message':f'Pakistan district analysis failed: {e}'}
    if uploaded_districts is not None:
        try:
            results['uploaded_boundaries'], rows['uploaded_boundaries'] = summarize_district_layer(
                uploaded_districts, boundary_wgs84, output_dir, landcover_path, utm,
                'uploaded_boundary_landcover_summary.csv', 'User-uploaded administrative polygons')
        except Exception as e:
            results['uploaded_boundaries']={'status':'error','message':f'Uploaded administrative boundary analysis failed: {e}'}
    return results, rows

@app.get('/')
def index(): return render_template('index.html')

@app.get('/api/status')
def status():
    return jsonify({'status':'ready','landcover_source':ESRI_LANDCOVER_URL,'dem_api_key_configured':bool(OPENTOPOGRAPHY_API_KEY),'cache_directory':'data/','accepted_uploads':['.shp','.shx','.dbf','.prj']})

@app.post('/api/analyze')
def analyze():
    job_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    job_dir = OUTPUTS / job_id; job_dir.mkdir(parents=True, exist_ok=True)
    try:
        if 'boundary_files' not in request.files: raise UserError('Select the .shp, .shx, .dbf and .prj files together.')
        stem, bundle = validate_sidecars(request.files.getlist('boundary_files'))
        upload_dir = UPLOADS / job_id; upload_dir.mkdir(parents=True, exist_ok=True)
        for ext, file in bundle.items(): file.save(upload_dir / f'{stem}{ext}')
        shp = upload_dir / f'{stem}.shp'
        try: raw = gpd.read_file(shp)
        except Exception as e: raise UserError(f'Could not read shapefile bundle: {e}. Ensure all four sidecars belong to the same shapefile.')
        wgs, projected, zone, utm, boundary_area = boundary_info(raw)
        # Optional second four-file shapefile bundle for district/admin polygons.
        uploaded_districts = None
        district_files = request.files.getlist('district_files')
        district_files = [f for f in district_files if f and f.filename]
        if district_files:
            d_stem, d_bundle = validate_sidecars(district_files)
            d_dir = UPLOADS / job_id / 'districts'; d_dir.mkdir(parents=True, exist_ok=True)
            for ext, file in d_bundle.items(): file.save(d_dir / f'{d_stem}{ext}')
            try: uploaded_districts = gpd.read_file(d_dir / f'{d_stem}.shp')
            except Exception as e: raise UserError(f'Could not read uploaded district shapefile bundle: {e}')
            if uploaded_districts.crs is None: raise UserError('Uploaded district boundary .prj is missing or invalid.')
            if not uploaded_districts.geometry.geom_type.isin(['Polygon','MultiPolygon']).all(): raise UserError('Uploaded district boundaries must contain only polygon/multipolygon features.')
        land_raw = job_dir / 'landcover_source.tif'
        esri = download_esri_landcover(wgs, land_raw)
        classes, valid_class_area, clipped, pixel_area = clip_and_summarize(land_raw, projected, utm, job_dir)
        preview, preview_bounds = create_landcover_preview(clipped, job_dir)
        zone_info, zone_rows = utm_zone_analysis(wgs, land_raw, job_dir)
        polygon_zone_info, polygon_zone_rows = boundary_zone_summary(wgs, job_dir)
        # Main dashboard statistics are aggregated from zone-specific UTM calculations,
        # avoiding one-zone distortion for polygons spanning several UTM zones.
        if zone_info.get('status') == 'success' and zone_rows:
            totals_by_class={}
            for zr in zone_rows:
                item=totals_by_class.setdefault(zr['class'],{'class':zr['class'],'color':zr['color'],'area_km2':0.0,'pixels':0})
                item['area_km2'] += float(zr['area_km2'])
            valid_class_area=sum(x['area_km2'] for x in totals_by_class.values())
            classes=[]
            for item in totals_by_class.values():
                item['area_km2']=round(item['area_km2'],6)
                item['percentage']=round(item['area_km2']/valid_class_area*100,4) if valid_class_area else 0
                classes.append(item)
            classes.sort(key=lambda x:x['area_km2'],reverse=True)
        dem_info, dem_path = fetch_dem(wgs, projected, utm, job_dir)
        district_info, district_rows = district_summary(wgs, job_dir, clipped, utm, uploaded_districts)
        pd.DataFrame(classes).to_csv(job_dir/'landcover_summary.csv', index=False)
        # Create a compact GeoJSON boundary for the web map.
        geojson_path = job_dir/'boundary.geojson'; wgs.to_file(geojson_path, driver='GeoJSON')
        result = {
            'job_id':job_id,'status':'success','created_utc':datetime.now(timezone.utc).isoformat(),
            'boundary_area_km2':round(boundary_area,4),'classified_area_km2':round(valid_class_area,4),
            'utm_zone':zone,'utm_epsg':utm.to_epsg(),'boundary_geojson':f'/outputs/{job_id}/boundary.geojson',
            'landcover_tif':f'/outputs/{job_id}/landcover_clipped.tif','landcover_preview':f'/outputs/{job_id}/landcover_preview.png','preview_bounds':preview_bounds,'summary_csv':f'/outputs/{job_id}/landcover_summary.csv',
            'district_csv':f'/outputs/{job_id}/pakistan_district_landcover_summary.csv' if (job_dir/'pakistan_district_landcover_summary.csv').exists() else None,
            'uploaded_district_csv':f'/outputs/{job_id}/uploaded_boundary_landcover_summary.csv' if (job_dir/'uploaded_boundary_landcover_summary.csv').exists() else None,
            'dem_tif':f'/outputs/{job_id}/dem_utm.tif' if dem_path else None,
            'classes':classes,'esri':esri,'dem':dem_info,'utm_zone_analysis':zone_info,'utm_zone_rows':zone_rows[:300],
            'polygon_zone_analysis':polygon_zone_info,'polygon_zone_rows':polygon_zone_rows[:500],
            'polygon_zone_csv':f'/outputs/{job_id}/boundary_polygon_utm_zones.csv' if (job_dir/'boundary_polygon_utm_zones.csv').exists() else None,
            'utm_zone_csv':f'/outputs/{job_id}/utm_zone_landcover_summary.csv' if (job_dir/'utm_zone_landcover_summary.csv').exists() else None,
            'districts':district_info,'district_rows':district_rows.get('pakistan_admin',[])[:100],
            'uploaded_district_rows':district_rows.get('uploaded_boundaries',[])[:100],
            'warnings':[]
        }
        if abs(valid_class_area-boundary_area)/max(boundary_area,1e-9) > 0.15:
            result['warnings'].append('Classified raster area differs substantially from polygon area. This may result from source coverage, raster resolution, no-data pixels, or export extent; percentages are relative to valid classified pixels.')
        if dem_info['status'] != 'success': result['warnings'].append(dem_info.get('message','Elevation unavailable.'))
        if zone_info.get('status') != 'success': result['warnings'].append(zone_info.get('message','UTM zone statistics unavailable.'))
        for key, info in district_info.items():
            if info.get('status') not in ('success','not_provided'): result['warnings'].append(info.get('message', f'{key} analysis unavailable.'))
        (job_dir/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        return jsonify(result)
    except UserError as e:
        return jsonify({'status':'error','error':str(e),'job_id':job_id}), 400
    except Exception as e:
        return jsonify({'status':'error','error':safe_error(e),'job_id':job_id}), 500

@app.get('/outputs/<job_id>/<path:filename>')
def outputs(job_id, filename):
    folder = (OUTPUTS / job_id).resolve()
    if OUTPUTS.resolve() not in folder.parents: return jsonify({'error':'Invalid path'}), 400
    if not (folder / filename).is_file(): return jsonify({'error':'Output not found'}), 404
    return send_from_directory(folder, filename, as_attachment=filename.lower().endswith(('.tif','.csv')))

@app.errorhandler(413)
def too_large(_): return jsonify({'status':'error','error':'Upload exceeds MAX_UPLOAD_MB limit.'}), 413

if __name__ == '__main__':
    app.run(host=os.getenv('HOST','127.0.0.1'), port=int(os.getenv('PORT','5000')), debug=False)
