Python
Copy
import urllib.request
import json

def get_approx_geolocation():
    """
    Fetches approximate geolocation (city, region, country, lat/lon)
    based on the public IP of the machine running the script.
    Uses the free ipapi.co API (no API key required, rate-limited).
    """
    try:
        with urllib.request.urlopen('https://ipapi.co/json/', timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            
        print("IP-based geolocation:")
        print(f"  IP:          {data.get('ip', 'N/A')}")
        print(f"  City:        {data.get('city', 'N/A')}")
        print(f"  Region:      {data.get('region', 'N/A')}")
        print(f"  Country:     {data.get('country_name', 'N/A')} ({data.get('country', 'N/A')})")
        print(f"  Coordinates: {data.get('latitude', 'N/A')}, {data.get('longitude', 'N/A')}")
        print(f"  Timezone:    {data.get('timezone', 'N/A')}")
        return data  # return full dict if you want to use it programmatically

    except Exception as e:
        print(f"Error fetching geolocation: {e}")
        return None

# Run it
if __name__ == "__main__":
    get_approx_geolocation()

How to use:

Save as geo_ip.py
Run: python geo_ip.py
No extra packages needed (pure stdlib).

Notes:

Accuracy is city-level at best (not exact street address).
Works anywhere with outbound HTTPS.
If you hit rate limits, alternatives: http://ip-api.com/json/ or https://ipinfo.io/json (swap the URL).
