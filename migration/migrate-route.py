import requests
import json
import re
import sys

# --- Configuration ---
# TODO: Update these variables to match your environment
KONG_ADMIN_URL = "http://localhost:8001"
APISIX_ADMIN_URL = "http://localhost:9180"
APISIX_API_KEY = "edd1c9f034335f136f87ad84b625c8f1"
PAGE_SIZE = 500


# --- Main Functions ---

def parse_kong_expression(expression):
    """
    Parses a Kong route expression string into an APISIX 'vars' list.
    """
    apisix_vars = []
    # Strip leading/trailing parentheses and split by '&&'
    conditions = expression.strip().strip('()').split('&&')

    for condition in conditions:
        condition = condition.strip()
        if not condition:
            continue

        # Regex to capture variable, operator, and value
        match = re.match(r'^(http\.[a-zA-Z0-9\._-]+)\s*(==|~)\s*(.*)$', condition)
        if not match:
            print(f"  -> Warning: Could not parse condition: '{condition}'", file=sys.stderr)
            continue

        kong_var, operator, value_raw = match.groups()
        # Clean up the value from quotes and special characters
        value = value_raw.strip().strip('r#').strip('"\'')
        apisix_var, apisix_op = "", ""

        # Translate Kong variables to APISIX variables
        if kong_var == "http.path":
            apisix_var = "request_uri"
        elif kong_var == "http.method":
            apisix_var = "request_method"
        elif kong_var.startswith("http.headers."):
            header_name = kong_var.split('.')[-1].replace('-', '_')
            apisix_var = f"http_{header_name}"

        # Translate operators
        if operator == "==":
            apisix_op = "=="
        elif operator == "~":
            apisix_op = "~~"

        if apisix_var and apisix_op:
            apisix_vars.append([apisix_var, apisix_op, value])
        else:
            print(f"  -> Warning: Failed to translate part of expression: '{condition}'", file=sys.stderr)

    return apisix_vars


def get_kong_routes_paginated():
    """
    Generator function to fetch Kong routes with pagination.
    """
    url = f"{KONG_ADMIN_URL}/routes?size={PAGE_SIZE}"
    headers = {}  # Add any necessary headers for Kong auth here

    while url:
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            routes = data.get('data', [])

            for route in routes:
                yield route

            # Get the next page URL, if it exists
            next_url = data.get('next')
            url = f"{KONG_ADMIN_URL}{next_url}" if next_url else None

        except requests.exceptions.RequestException as e:
            print(f"Error fetching routes from Kong: {e}", file=sys.stderr)
            break  # Exit if there's an error fetching routes


def migrate_routes():
    """
    Main function to fetch Kong routes and migrate them to APISIX.
    """
    print("Starting Kong to APISIX migration...")

    # Use the paginated generator to process all routes
    for route in get_kong_routes_paginated():
        route_id = route.get('id')
        route_name = route.get('name')
        service_id = route.get('service', {}).get('id')
        expression = route.get('expression')

        print("-----------------------------------------------------")
        print(f"Processing Kong Route ID: {route_id} (Name: {route_name or 'N/A'})")

        if not service_id:
            print("  -> Warning: Route has no service. Skipping.", file=sys.stderr)
            continue

        apisix_payload = {"name": route_name or route_id, "id": route_id}

        if expression:
            print(f"  -> Found expression: {expression}")
            apisix_vars = parse_kong_expression(expression)
            print(f"  -> Translated to APISIX vars: {json.dumps(apisix_vars)}")
            if apisix_vars:
                apisix_payload['vars'] = apisix_vars
            else:
                print("  -> Error: Expression parsing failed. Skipping.", file=sys.stderr)
                continue

        # Keep original paths if they exist, otherwise default
        uris = route.get('paths') or ["/*"]
        apisix_payload['uris'] = uris

        try:
            # Fetch the associated service from Kong
            response = requests.get(f"{KONG_ADMIN_URL}/services/{service_id}")
            response.raise_for_status()
            kong_service = response.json()
        except requests.exceptions.RequestException as e:
            print(f"  -> Warning: Could not fetch service {service_id}. Skipping. {e}", file=sys.stderr)
            continue

        # Construct the upstream information for APISIX
        apisix_payload['upstream'] = {
            "scheme": kong_service.get('protocol', 'http'),
            "type": "roundrobin",
            "nodes": {f"{kong_service.get('host')}:{kong_service.get('port')}": 1}
        }

        print("  -> APISIX Payload:")
        print(json.dumps(apisix_payload, indent=2))

        try:
            # Create or update the route in APISIX using PUT
            headers = {'X-API-KEY': APISIX_API_KEY, 'Content-Type': 'application/json'}
            response = requests.put(
                f"{APISIX_ADMIN_URL}/apisix/admin/routes/{route_id}",
                headers=headers,
                json=apisix_payload
            )
            response.raise_for_status()
            print(f"  -> Successfully migrated route {route_id}. Status: {response.status_code}")

        except requests.exceptions.RequestException as e:
            status_code = e.response.status_code if e.response else 'N/A'
            response_text = e.response.text if e.response else 'No response from server.'
            print(f"  -> Error creating route {route_id} in APISIX. Status: {status_code}", file=sys.stderr)
            print(f"     Response: {response_text}", file=sys.stderr)

    print("-----------------------------------------------------")
    print("Migration process finished.")


if __name__ == "__main__":
    migrate_routes()
