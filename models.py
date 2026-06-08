from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Account(db.Model):
    __tablename__ = 'accounts'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    api_key = db.Column(db.String(255), nullable=False)
    api_secret = db.Column(db.String(255), nullable=False)
    leverage = db.Column(db.Integer, default=50, nullable=False)
    balance_buffer_pct = db.Column(db.Float, default=55.0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "api_key": self.api_key,
            "api_secret": self.api_secret[:6] + "..." if self.api_secret else "",
            "leverage": self.leverage,
            "balance_buffer_pct": self.balance_buffer_pct,
            "is_active": self.is_active
        }

class GlobalSetting(db.Model):
    __tablename__ = 'settings'
    
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.String(255), nullable=False)

class TradeLog(db.Model):
    __tablename__ = 'trade_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=db.func.now(), nullable=False)
    ticker = db.Column(db.String(50), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    source = db.Column(db.String(50), default="webhook", nullable=False) # "webhook" or "email_fallback"
    status = db.Column(db.String(50), nullable=False) # "success", "failed", "verified"
    details = db.Column(db.Text, nullable=True) # JSON string or text details
    
    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "ticker": self.ticker,
            "action": self.action,
            "source": self.source,
            "status": self.status,
            "details": self.details
        }
