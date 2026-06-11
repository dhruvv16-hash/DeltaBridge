import os
from dotenv import load_dotenv

# Load local .env file if it exists
load_dotenv()

class Config:
    # Delta Exchange API Credentials
    API_KEY = os.getenv("DELTA_API_KEY")
    API_SECRET = os.getenv("DELTA_API_SECRET")
    
    # Delta Exchange Base URL
    # Global Production: https://api.delta.exchange
    # India Production: https://api.india.delta.exchange
    # Testnet: https://api.testnet.delta.exchange
    BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.delta.exchange").rstrip("/")
    
    # Security Passphrase for Webhooks (must match what's set in TradingView message)
    PASSPHRASE = os.getenv("PASSPHRASE")
    
    # Trading Defaults
    DEFAULT_LEVERAGE = 50
    try:
        DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "50"))
    except ValueError:
        pass
    
    # Risk Management / Margin Buffer
    # percentage of available balance to allocate per trade (e.g. 95 means 95%, leaving a 5% buffer for fees/slippage)
    BALANCE_BUFFER_PCT = 0.95
    try:
        _raw_buf = os.getenv("BALANCE_BUFFER_PCT", "95")
        if isinstance(_raw_buf, str):
            _raw_buf = _raw_buf.replace("%", "").strip()
        BALANCE_BUFFER_PCT = float(_raw_buf) / 100.0
    except ValueError:
        pass

    # Trading Symbol / Asset Configuration (e.g. BTCUSD.P, SOLUSD.P)
    _raw_symbol = os.getenv("TRADING_SYMBOL", "")
    TRADING_SYMBOL = "" if _raw_symbol == "symbol" else _raw_symbol

    @classmethod
    def validate(cls):
        """Validates that all essential config variables are set."""
        missing = []
        if not cls.API_KEY:
            missing.append("DELTA_API_KEY")
        if not cls.API_SECRET:
            missing.append("DELTA_API_SECRET")
        if not cls.PASSPHRASE:
            missing.append("PASSPHRASE")
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
