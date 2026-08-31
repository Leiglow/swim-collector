"""Every upstream data source for the bathing water tool, in one place.

Each URL here was called and verified on 2026-08-29. Where a source has a trap
in it, the trap is written down next to the URL rather than in a commit message,
because the traps are the kind that produce a *plausible wrong answer* rather
than an error — telling someone the sea is clean when a pipe is discharging.

Two rules learned the hard way and encoded below:

  * ArcGIS silently truncates at maxRecordCount and sets exceededTransferLimit.
    Never trust a feature count from a single call — page until it says stop.
  * "Storm Overflow Activity" services are live. "Event Duration Monitoring
    <year>" services are last year's annual return with no Status field at all.
    Mixing them up would put a green dot on a discharging outfall.
"""

# ---------------------------------------------------------------------------
# Bathing water registers — who is a designated bathing water, and where
# ---------------------------------------------------------------------------

# England. No CORS header, so this must be fetched server-side (it is).
# Paging is _pageSize/_page; _limit is rejected and _view=full 400s.
EA_SITES = "https://environment.data.gov.uk/doc/bathing-water.json?_pageSize=2000"

# Wales is NOT in the England register despite the legacy "England and Wales"
# label on it — it is a parallel dataset under /wales/bathing-waters/.
NRW_SITES = "https://environment.data.gov.uk/wales/bathing-waters/doc/bathing-water.json?_pageSize=500"

# Wales classifications: /data/ endpoints reject _page/_pageSize — call bare.
# Results live under result.primaryTopic.observation[], not result.items[].
NRW_CLASS = "https://environment.data.gov.uk/wales/bathing-waters/data/bathing-water-quality/compliance-rBWD/slice/year/{year}.json"

# Past classifications, for the trend on a beach page. The registers give only
# the LATEST assessment, and a single letter grade cannot tell you whether a
# place is getting better or worse — which is the question a rating four summers
# wide is actually able to answer.
#
# Note the parameter: sampleYear=2024 returns a 500 and year=2024 a 400. The
# nested form is the one that works.
EA_CLASS_YEAR = ("https://environment.data.gov.uk/doc/bathing-water-quality/"
                 "compliance-rBWD.json?sampleYear.ordinalYear={year}&_pageSize=2000")
NRW_CLASS_YEAR = ("https://environment.data.gov.uk/wales/bathing-waters/doc/"
                  "bathing-water-quality/compliance-rBWD.json"
                  "?sampleYear.ordinalYear={year}&_pageSize=1000")
CLASS_YEARS = 5

# Scotland. Native projection is EPSG:27700, so outSR=4326 is required.
SEPA_SITES = ("https://map.sepa.org.uk/server/rest/services/Open/Environmental_Monitoring"
              "/MapServer/1/query?where=1%3D1&outFields=*&outSR=4326&f=geojson")

# Northern Ireland — all 33 sites, and the only live NI signal there is.
DAERA_SITES = ("https://services-eu1.arcgis.com/kswen6BYexuc1SUk/arcgis/rest/services"
               "/Bathing_Water_Monitoring_Points_Public_View_PRD/FeatureServer/0/query"
               "?where=1%3D1&outFields=*&f=geojson&outSR=4326")

# Republic of Ireland. Undocumented API behind beaches.ie — no stability promise.
# COORDINATE TRAP: EtrsY (latitude) is null on 87 of 240 records, and some EtrsX
# values are rounded placeholders. Only ~153 have a usable pair; the rest are
# dropped rather than guessed at.
ROI_SITES = "https://api.beaches.ie/odata/beaches"

# ---------------------------------------------------------------------------
# Live signals — the "is it safe today" layer
# ---------------------------------------------------------------------------

# England's daily pollution risk forecast. The predictedOn filter is MANDATORY:
# without it the endpoint returns the whole historical archive in arbitrary
# order, so an unfiltered call can hand you a forecast from last September.
EA_PRF = ("https://environment.data.gov.uk/doc/bathing-water-quality/stp-risk-prediction.json"
          "?predictedOn={date}&_pageSize=1000")

# Same endpoint under the Wales prefix.
NRW_PRF = ("https://environment.data.gov.uk/wales/bathing-waters/doc/bathing-water-quality"
           "/stp-risk-prediction.json?predictedOn={date}&_pageSize=500")

# Scotland's daily prediction. Undocumented file backing a table widget, no CORS.
# Covers only 30 of the 90 Scottish sites — the other 60 have annual data only.
SEPA_PREDICTIONS = "https://bathingwaters.sepa.org.uk/json/currentstatus.json"

# Republic of Ireland: statutory swimming restrictions. This is the strongest
# signal in the whole system — a legal prohibition, not a model output.
# The path segment is a "top N" limit, not an id.
ROI_RESTRICTIONS = "https://api.beaches.ie/api/beach/restricted/500"

# England weekly bacterial samples, one call for every site in an ISO week.
EA_SAMPLES = ("https://environment.data.gov.uk/doc/bathing-water-quality/in-season/sample.json"
              "?_pageSize=1000&sampleWeek=http%3A%2F%2Freference.data.gov.uk%2Fid%2Fweek%2F{week}")

# ---------------------------------------------------------------------------
# Live storm overflow feeds
# ---------------------------------------------------------------------------
#
# STATUS CODES, read off the services' own coded-value domains and confirmed
# against live discharging records:  1 = discharging now
#                                    0 = not discharging
#                                   -1 = offline / under maintenance
#
# An early note in our research had this inverted. It is written here explicitly
# because getting it backwards paints every clean beach red and every spill green.

# The eight English companies on the Water UK common schema. One adapter, eight
# URLs. Field-name case differs: South West is lowerCamelCase, the rest TitleCase,
# so the adapter matches field names case-insensitively.
EDM_FEEDS = {
    "Thames Water":
        "https://services2.arcgis.com/g6o32ZDQ33GpCIu3/arcgis/rest/services"
        "/Thames_Water_Storm_Overflow_Activity_%28Production%29_view/FeatureServer/0/query",
    "South West Water":
        "https://services-eu1.arcgis.com/OMdMOtfhATJPcHe3/arcgis/rest/services"
        "/NEH_outlets_PROD/FeatureServer/0/query",
    "Wessex Water":
        "https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services"
        "/Wessex_Water_Storm_Overflow_Activity/FeatureServer/0/query",
    "United Utilities":
        "https://services5.arcgis.com/5eoLvR0f8HKb7HWP/arcgis/rest/services"
        "/United_Utilities_Storm_Overflow_Activity/FeatureServer/0/query",
    "Yorkshire Water":
        "https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services"
        "/Yorkshire_Water_Storm_Overflow_Activity/FeatureServer/0/query",
    "Northumbrian Water":
        "https://services-eu1.arcgis.com/MSNNjkZ51iVh8yBj/arcgis/rest/services"
        "/Northumbrian_Water_Storm_Overflow_Activity_2_view/FeatureServer/0/query",
    "Anglian Water":
        "https://services3.arcgis.com/VCOY1atHWVcDlvlJ/arcgis/rest/services"
        "/stream_service_outfall_locations_view/FeatureServer/0/query",
    "Severn Trent":
        "https://services1.arcgis.com/NO7lTIlnxRMMG9Gw/arcgis/rest/services"
        "/Severn_Trent_Water_Storm_Overflow_Activity/FeatureServer/0/query",
}

# Southern Water is the ninth English company and does NOT use the common schema.
# ReleaseStatus is a 1/2/3 traffic light, NOT a discharge state — the only way to
# know something is discharging now is the plain-English SpillMessage field.
SOUTHERN_OUTFALLS = ("https://services-eu1.arcgis.com/6qJmARkS2dt2IjVA/arcgis/rest/services"
                     "/COR_RW_RSW_DATA_VIEW/FeatureServer/1/query")
# Southern's own bathing-site view, which applies a tidal model. Carries eubwid,
# so it joins straight to the EA register.
SOUTHERN_SITES = ("https://services-eu1.arcgis.com/6qJmARkS2dt2IjVA/arcgis/rest/services"
                  "/COR_RW_RSW_DATA_VIEW/FeatureServer/0/query")
SOUTHERN_SPILLING = "There is an ongoing"      # SpillMessage prefix meaning: discharging now

# Wales. Ships Linked_Bathing_Water — Welsh Water's own outfall-to-beach mapping,
# which beats any distance guess we could make. Comma-separated string, not a list.
DCWW_SPILLS = ("https://services3.arcgis.com/KLNF7YxtENPLYVey/arcgis/rest/services"
               "/Spill_Prod__view/FeatureServer/0/query")
DCWW_STATUS = ("https://services3.arcgis.com/KLNF7YxtENPLYVey/arcgis/rest/services"
               "/OverflowMapStatus_Layer_PROD_view/FeatureServer/0/query")

# Scotland. The official API is an hour fresh; the ArcGIS mirror of it was 11
# hours stale when tested, so the official one is used despite having no CORS
# (we fetch server-side anyway) and no filtering at all — it is one 2.9MB blob.
SW_NRT = "https://api.scottishwater.co.uk/overflow-event-monitoring/v1/near-real-time"
SW_SPILLING = "OF"          # OF = Overflowing, RO = Recent Overflow, NO = none, DA = no data

# Ireland, north and south, has no live spill monitoring of any kind. There is no
# EDM programme. The tool must say so rather than implying an absence of data is
# an absence of sewage.

# ---------------------------------------------------------------------------
# Rain — the best available proxy for a spill nobody has reported yet
# ---------------------------------------------------------------------------
# Free tier is NON-COMMERCIAL. Fine for a private tool; revisit before any
# commercial launch. Response shape changes from object to array above one
# coordinate, so callers must branch on that.
# forecast_days=2, not 1: with a single day the "next 24 hours" figure is really
# "the rest of today", and shrinks to nothing by the evening.
# forecast_days=7 and the daily block ride along on the call that was already
# being made for rainfall, so the week ahead costs NOTHING extra. Open-Meteo
# weights a request by locations, and only counts extra when a single location
# asks for more than 10 variables or more than two weeks: this is six variables
# over nine days, so it stays one call per location exactly as before.
OPEN_METEO = ("https://api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}"
              "&hourly=precipitation"
              "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
              "precipitation_sum,wind_speed_10m_max"
              "&past_days=2&forecast_days=7&timezone=GMT")
OPEN_METEO_BATCH = 100

# Sea temperature comes from a different service. It only answers for points at
# sea: ask it about a river or a lake and every value comes back null, which is
# the right answer and is shown as nothing rather than guessed at.
OPEN_METEO_MARINE = ("https://marine-api.open-meteo.com/v1/marine?latitude={lats}"
                     "&longitude={lons}&daily=sea_surface_temperature_max,"
                     "sea_surface_temperature_min&forecast_days=7&timezone=GMT")

USER_AGENT = "swim-collector/1.0 (personal bathing water tool; open data)"
