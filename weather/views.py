from django.shortcuts import render

# weather/views.py
# weather/views.py
from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .services.open_meteo import geocode, forecast, current_only


def _cache_key(prefix: str, **kwargs) -> str:
    parts = [prefix] + [f"{k}={kwargs[k]}" for k in sorted(kwargs.keys())]
    return "|".join(parts)


class LocationSearchView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        country = (request.query_params.get("country") or "VN").strip().upper()
        limit = request.query_params.get("limit") or "10"
        lang = request.query_params.get("lang") or "vi"

        ck = _cache_key("wx:geo", q=q, country=country, limit=limit, lang=lang)
        cached = cache.get(ck)
        if cached is not None:
            return Response(cached)

        try:
            data = geocode(q, country_code=country, count=int(limit), language=lang)
            results = data.get("results") or []
            out = [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "admin1": r.get("admin1"),
                    "admin2": r.get("admin2"),
                    "country": r.get("country"),
                    "country_code": r.get("country_code"),
                    "latitude": r.get("latitude"),
                    "longitude": r.get("longitude"),
                    "timezone": r.get("timezone"),
                    "population": r.get("population"),
                }
                for r in results
            ]
            payload = {"results": out}
            cache.set(ck, payload, timeout=3600)
            return Response(payload)
        except Exception as e:
            return Response(
                {"detail": "Geocoding failed", "error": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class ForecastView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        lat = request.query_params.get("lat")
        lon = request.query_params.get("lon")
        tz = request.query_params.get("tz") or "Asia/Ho_Chi_Minh"
        days = request.query_params.get("days") or "7"

        if lat is None or lon is None:
            return Response({"detail": "Missing lat/lon"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            lat_f = float(lat)
            lon_f = float(lon)
            days_i = int(days)
        except Exception:
            return Response({"detail": "Invalid lat/lon/days"}, status=status.HTTP_400_BAD_REQUEST)

        ck = _cache_key("wx:fc", lat=round(lat_f, 4), lon=round(lon_f, 4), tz=tz, days=days_i)
        cached = cache.get(ck)
        if cached is not None:
            return Response(cached)

        try:
            data = forecast(lat_f, lon_f, timezone=tz, days=days_i)
            cache.set(ck, data, timeout=600)
            return Response(data)
        except Exception as e:
            return Response(
                {"detail": "Forecast failed", "error": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )


VN_POINTS = [
    # name, lat, lon (xấp xỉ)
    ("Hà Nội", 21.0278, 105.8342),
    ("TP HCM", 10.8231, 106.6297),
    ("Đà Nẵng", 16.0544, 108.2022),
    ("Hải Phòng", 20.8449, 106.6881),
    ("Cần Thơ", 10.0452, 105.7469),
    ("An Giang", 10.5216, 105.1259),
    ("Bà Rịa - Vũng Tàu", 10.5417, 107.2420),
    ("Bắc Giang", 21.2731, 106.1946),
    ("Bắc Kạn", 22.1470, 105.8340),
    ("Bạc Liêu", 9.2940, 105.7216),
    ("Bắc Ninh", 21.1861, 106.0763),
    ("Bến Tre", 10.2434, 106.3756),
    ("Bình Định", 13.7820, 109.2190),
    ("Bình Dương", 11.3254, 106.4770),
    ("Bình Phước", 11.7512, 106.7235),
    ("Bình Thuận", 10.9289, 108.1021),
    ("Cà Mau", 9.1769, 105.1524),
    ("Cao Bằng", 22.6666, 106.2639),
    ("Đắk Lắk", 12.6667, 108.0500),
    ("Đắk Nông", 12.0040, 107.6900),
    ("Điện Biên", 21.3833, 103.0167),
    ("Đồng Nai", 10.9574, 106.8427),
    ("Đồng Tháp", 10.5370, 105.6730),
    ("Gia Lai", 13.9833, 108.0000),
    ("Hà Giang", 22.8333, 104.9833),
    ("Hà Nam", 20.5835, 105.9220),
    ("Hà Tĩnh", 18.3559, 105.8877),
    ("Hải Dương", 20.9373, 106.3146),
    ("Hậu Giang", 9.7845, 105.4701),
    ("Hòa Bình", 20.8172, 105.3376),
    ("Hưng Yên", 20.6464, 106.0511),
    ("Khánh Hòa", 12.2388, 109.1967),
    ("Kiên Giang", 10.0125, 105.0809),
    ("Kon Tum", 14.3545, 108.0076),
    ("Lai Châu", 22.3862, 103.4703),
    ("Lâm Đồng", 11.9465, 108.4419),
    ("Lạng Sơn", 21.8537, 106.7610),
    ("Lào Cai", 22.3381, 104.1487),
    ("Long An", 10.6956, 106.2431),
    ("Nam Định", 20.4388, 106.1621),
    ("Nghệ An", 18.6730, 105.6923),
    ("Ninh Bình", 20.2506, 105.9745),
    ("Ninh Thuận", 11.5653, 108.9886),
    ("Phú Thọ", 21.3227, 105.4010),
    ("Phú Yên", 13.0882, 109.0929),
    ("Quảng Bình", 17.4689, 106.6223),
    ("Quảng Nam", 15.5394, 108.0191),
    ("Quảng Ngãi", 15.1200, 108.7920),
    ("Quảng Ninh", 21.0064, 107.2925),
    ("Quảng Trị", 16.8162, 107.1000),
    ("Sóc Trăng", 9.6033, 105.9800),
    ("Sơn La", 21.3280, 103.9140),
    ("Tây Ninh", 11.3352, 106.1099),
    ("Thái Bình", 20.4463, 106.3366),
    ("Thái Nguyên", 21.5928, 105.8442),
    ("Thanh Hóa", 19.8070, 105.7760),
    ("Thừa Thiên Huế", 16.4637, 107.5909),
    ("Tiền Giang", 10.3600, 106.3500),
    ("Trà Vinh", 9.9347, 106.3451),
    ("Tuyên Quang", 21.8236, 105.2142),
    ("Vĩnh Long", 10.2396, 105.9572),
    ("Vĩnh Phúc", 21.3089, 105.6049),
    ("Yên Bái", 21.7229, 104.9113),
]

class VietnamHeatmapView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        tz = request.query_params.get("tz") or "Asia/Ho_Chi_Minh"
        ck = "wx:vn:heatmap:v1"
        cached = cache.get(ck)
        if cached is not None:
            return Response(cached)

        def worker(name, lat, lon):
            data = current_only(lat, lon, timezone=tz)
            cur = data.get("current") or {}
            return {
                "name": name,
                "latitude": lat,
                "longitude": lon,
                "temperature": cur.get("temperature_2m"),
                "weather_code": cur.get("weather_code"),
            }

        results = []
        try:
            with ThreadPoolExecutor(max_workers=12) as ex:
                futs = [ex.submit(worker, n, la, lo) for (n, la, lo) in VN_POINTS]
                for f in as_completed(futs):
                    results.append(f.result())

            results.sort(key=lambda x: x["name"])
            payload = {
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "unit": "°C",
                "results": results,
            }
            cache.set(ck, payload, timeout=900)  # 15 phút
            return Response(payload)
        except Exception as e:
            return Response({"detail": "Heatmap failed", "error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)