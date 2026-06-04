import sys
from config import Config
from delta_client import DeltaClient

def main():
    print("Initializing integration test...")
    try:
        # Load and validate configs
        Config.validate()
    except Exception as e:
        print(f"[ERROR] Configuration Error: {e}")
        print("Please make sure you have filled in DELTA_API_KEY, DELTA_API_SECRET, and PASSPHRASE in the .env file.")
        sys.exit(1)

    print(f"Connecting to Delta Exchange at: {Config.BASE_URL}")
    client = DeltaClient(
        api_key=Config.API_KEY,
        api_secret=Config.API_SECRET,
        base_url=Config.BASE_URL
    )

    print("Fetching live wallet balances...")
    balance, asset = client.get_available_balance()
    
    # Check if we got an error or successfully fetched balance
    # If the signature was invalid, get_available_balance prints error logs and returns 0.0, "USDT"
    # Let's perform a direct raw check to print the full API response for detailed feedback
    response = client._request("GET", "/v2/wallet/balances", is_private=True)
    
    if response.get("success"):
        print("\n[SUCCESS] API Connection: SUCCESSFUL!")
        print(f"Your live balance is: {balance} {asset}")
        print("\nFull wallet details:")
        for bal in response.get("result", []):
            symbol = bal.get("asset_symbol")
            total = bal.get("balance")
            avail = bal.get("available_balance")
            print(f" - {symbol}: Total = {total}, Available = {avail}")
    else:
        print("\n[FAILED] API Connection: FAILED")
        print(f"Error Details: {response.get('error') or response}")
        print("\nTroubleshooting tips:")
        print("1. Double check your DELTA_API_KEY and DELTA_API_SECRET in the .env file.")
        print("2. Ensure your computer's system clock is accurate (synced to internet time).")
        print("3. Verify that your API Key is active and has correct permissions on Delta Exchange.")

if __name__ == "__main__":
    main()
