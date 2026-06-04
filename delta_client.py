import time
import hmac
import hashlib
import json
import requests
import logging

logger = logging.getLogger(__name__)

class DeltaClient:
    def __init__(self, api_key, api_secret, base_url="https://api.delta.exchange"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self._products_cache = None
        self._products_cache_time = 0
        
    def _generate_headers(self, method, path, query_string="", body=""):
        """
        Generates HMAC-SHA256 signature and request headers for private endpoints.
        Signature = HMAC-SHA256(API_SECRET, METHOD + TIMESTAMP + PATH + QUERY_STRING + BODY)
        """
        timestamp = str(int(time.time()))
        
        # Format the query string properly
        if query_string:
            if not query_string.startswith('?'):
                query_string = '?' + query_string
        else:
            query_string = ""
            
        signature_data = f"{method.upper()}{timestamp}{path}{query_string}{body}"
        
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            signature_data.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        headers = {
            'api-key': self.api_key,
            'timestamp': timestamp,
            'signature': signature,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        return headers

    def _request(self, method, path, query_string="", payload=None, is_private=True):
        """Sends an HTTP request to the Delta Exchange API."""
        url = f"{self.base_url}{path}"
        if query_string:
            if not query_string.startswith('?'):
                url += f"?{query_string}"
            else:
                url += query_string
                
        body = ""
        if payload is not None:
            # Sort keys and remove whitespace for reliable signing
            body = json.dumps(payload, separators=(',', ':'))
            
        if is_private:
            headers = self._generate_headers(method, path, query_string, body)
        else:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
        logger.debug(f"Request: {method} {url} Headers: {headers} Body: {body}")
        
        try:
            if method.upper() == "GET":
                response = requests.get(url, headers=headers, timeout=10)
            elif method.upper() == "POST":
                response = requests.post(url, headers=headers, data=body, timeout=10)
            elif method.upper() == "DELETE":
                response = requests.delete(url, headers=headers, data=body, timeout=10)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
                
            response_json = response.json()
            if not response.ok or not response_json.get("success", False):
                logger.error(f"Delta API error response ({response.status_code}): {response.text}")
                return response_json
            return response_json
        except Exception as e:
            logger.error(f"Exception during request to {url}: {e}")
            return {"success": False, "error": str(e)}

    def get_products(self, force_refresh=False):
        """Fetches and caches all available trading products (public endpoint)."""
        # Cache products for 1 hour to avoid rate limits
        now = time.time()
        if self._products_cache and (now - self._products_cache_time < 3600) and not force_refresh:
            return self._products_cache
            
        logger.info("Fetching products list from Delta Exchange...")
        response = self._request("GET", "/v2/products", is_private=False)
        if response.get("success"):
            self._products_cache = response.get("result", [])
            self._products_cache_time = now
            return self._products_cache
        return []

    def get_product_by_symbol(self, symbol):
        """Normalizes symbol and retrieves the product specifications."""
        # Replace .PERP first, then .P, to avoid partial replacement bugs
        normalized_symbol = symbol.upper().replace(".PERP", "").replace(".P", "").replace("/", "")
        products = self.get_products()
        
        # Try to find exact match
        for p in products:
            p_symbol = p.get("symbol", "").upper()
            if p_symbol == normalized_symbol or p_symbol == f"{normalized_symbol}T":
                return p
        
        # Fallback to standard check
        for p in products:
            if normalized_symbol in p.get("symbol", "").upper():
                return p
                
        return None

    def get_ticker(self, symbol):
        """Fetches ticker details for a given symbol (public endpoint)."""
        # Replace .PERP first, then .P, to avoid partial replacement bugs
        normalized_symbol = symbol.upper().replace(".PERP", "").replace(".P", "").replace("/", "")
        path = f"/v2/tickers/{normalized_symbol}"
        response = self._request("GET", path, is_private=False)
        if response.get("success"):
            return response.get("result")
        
        # Fallback to fetching all tickers if specific symbol lookup fails
        logger.warning(f"Ticker lookup for {normalized_symbol} failed, falling back to all tickers...")
        all_tickers_response = self._request("GET", "/v2/tickers", is_private=False)
        if all_tickers_response.get("success"):
            tickers = all_tickers_response.get("result", [])
            for t in tickers:
                if t.get("symbol", "").upper() == normalized_symbol:
                    return t
        return None

    def get_available_balance(self):
        """
        Retrieves the wallet balances and returns the highest available stablecoin (USDT/USDC) balance.
        """
        response = self._request("GET", "/v2/wallet/balances", is_private=True)
        if not response.get("success"):
            logger.error(f"Failed to fetch wallet balances: {response.get('error')}")
            return 0.0, "USDT"
            
        balances = response.get("result", [])
        available_balance = 0.0
        settling_asset = "USDT"
        
        for bal in balances:
            asset = bal.get("asset_symbol", "").upper()
            try:
                avail = float(bal.get("available_balance", 0.0))
            except (ValueError, TypeError):
                avail = 0.0
                
            if asset in ["USDT", "USDC", "USD", "DETO"] and avail > available_balance:
                available_balance = avail
                settling_asset = asset
                
        logger.info(f"Retrieved balance: {available_balance} {settling_asset}")
        return available_balance, settling_asset

    def get_position(self, product_id):
        """Fetches the current open position size for a specific product ID."""
        # Using products filter in query parameter
        query_string = f"product_ids={product_id}"
        response = self._request("GET", "/v2/positions/margined", query_string=query_string, is_private=True)
        
        if not response.get("success"):
            logger.error(f"Failed to fetch position for product_id {product_id}: {response.get('error')}")
            return None
            
        positions = response.get("result", [])
        for pos in positions:
            if int(pos.get("product_id", 0)) == int(product_id):
                return pos
        return None

    def place_order(self, product_id, size, side, order_type="market_order", limit_price=None, reduce_only=False):
        """Places an order on Delta Exchange."""
        payload = {
            "product_id": int(product_id),
            "size": int(size),
            "side": side.lower(),
            "order_type": order_type,
            "reduce_only": bool(reduce_only)
        }
        
        if order_type == "limit_order" and limit_price is not None:
            payload["limit_price"] = str(limit_price)
            
        logger.info(f"Placing order: {side.upper()} {size} contracts of product {product_id} (Type: {order_type}, ReduceOnly: {reduce_only})")
        response = self._request("POST", "/v2/orders", payload=payload, is_private=True)
        return response
