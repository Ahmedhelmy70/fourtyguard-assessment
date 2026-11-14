import requests

URL = "https://api.fortyguard.com/v1/heatmap"

headers = {
        "api-key": "3d7b8c26d3056b56ec7290c9a50cc663",
        "Content-Type": "application/json"
}


response = requests.get(URL, headers=headers)
