# Pakistan LandCover 3D GIS

A local Flask GIS application for polygon-based land-cover analysis in Pakistan and other areas covered by the configured Esri ImageServer. It provides a Leaflet 2D map, a MapLibre 3D terrain view, land-cover class statistics, UTM-zone summaries, district summaries, and downloadable GIS outputs.

## Features

- Upload a polygon shapefile bundle (`.shp`, `.shx`, `.dbf`, `.prj`) for a study area.
- Optional second shapefile bundle for user-provided administrative/district polygons.
- Request classified land-cover data from the configured Esri ImageServer, clip it to the boundary, and calculate area/percentage summaries.
- Calculate UTM-zone and district statistics where data are available.
- Optional elevation download through OpenTopography when an API key is configured; otherwise the application may try its public Copernicus GLO-90 tile fallback.
- View the boundary and classified raster overlay on a 2D map and a 3D terrain view.
- Download the result GeoTIFFs and CSV summaries.

## Requirements

- Windows 10/11, 64-bit Python 3.11 recommended.
- Internet access for the Esri service, map tiles, GADM boundaries, and optional elevation sources.
- A valid polygon shapefile bundle. All four files must share the same base name.

## Run locally on Windows

1. Download or clone this repository.
2. Open Command Prompt in the repository folder.
3. Run:

```bat
py -3.11 -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

4. If you have an OpenTopography API key, edit `.env` and replace `PUT_YOUR_API_KEY_HERE` with your key. Do not commit `.env`.
5. Start the app:

```bat
python app.py
```

Or double-click `run_app.bat` on Windows. Open `http://127.0.0.1:5000` in Chrome or Edge.

## Upload rules

Choose all four study-boundary files together. For example, `study_area.shp`, `study_area.shx`, `study_area.dbf`, and `study_area.prj`. The optional district layer follows the same rule. ZIP files, KML/KMZ, GeoJSON, and other vector formats are not accepted by this version. A valid projection definition (`.prj`) is required.

## Environment settings

See `.env.example`:

- `ESRI_LANDCOVER_IMAGE_SERVER_URL`: Esri ImageServer endpoint.
- `OPENTOPOGRAPHY_API_KEY`: optional key for OpenTopography COP30 elevation downloads.
- `GADM_PAK_GPKG_URL`: GADM GeoPackage download URL.
- `MAX_UPLOAD_MB`: maximum request size in megabytes.
- `HOST` and `PORT`: local server binding. The default is local-only (`127.0.0.1:5000`).

## Data and accuracy notes

- Land-cover results depend on the configured Esri service, its current content, export limits, and service availability. Large areas may be downsampled to fit service limits; inspect the reported export dimensions before describing results as native 10 m.
- Areas are calculated in projected coordinate systems from raster pixel dimensions. Classified area can differ from polygon area due to no-data pixels, source coverage, resolution, and edge effects.
- GADM is a non-official administrative boundary source. Verify district boundaries and names against authoritative sources before official reporting.
- COP30 is a digital surface model product; surface objects may affect elevations. The fallback GLO-90 product is coarser.
- The app uses public online map/elevation tile providers. Their availability and terms are controlled by those providers.
- This is a local-development application, not a hardened public multi-user service. Do not expose it directly to the public internet without authentication, CSRF protection, request limits, storage cleanup, a production WSGI server, and a security review.

## Repository structure

```text
Pakistan_LandCover_3D_GIS/
├── app.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── run_app.bat
├── templates/
│   └── index.html
├── static/
├── data/       # created at runtime; ignored by Git
├── uploads/    # created at runtime; ignored by Git
└── outputs/    # created at runtime; ignored by Git
```

## Troubleshooting

- **Dependency install errors:** use 64-bit Python 3.11 in a clean virtual environment. GeoPandas/Rasterio include compiled geospatial dependencies; Conda-forge can be a good alternative if pip installation fails.
- **Esri export fails:** verify internet access and the configured ImageServer endpoint.
- **No classified pixels:** verify that the shapefile has the correct CRS, valid polygon geometry, and intersects the service coverage.
- **DEM is unavailable:** configure a valid OpenTopography key in `.env`, or check the returned message for fallback tile download details.
- **District summaries unavailable:** the GADM download or layer names may have changed. Upload your own district polygons if needed.
