from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Account(db.Model):
    __tablename__ = 'accounts'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    api_key = db.Column(db.String(255), nullable=False)
    api_secret = db.Column(db.String(255), nullable=False)
    leverage = db.Column(db.Integer, default=50, nullable=False)
    balance_buffer_pct = db.Column(db.Float, default=95.0, nullable=False)
    sizing_type = db.Column(db.String(20), default="percentage", nullable=False) # "percentage" or "fixed"
    fixed_amount = db.Column(db.Float, default=10.0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    daily_loss_limit = db.Column(db.Float, nullable=True)
    is_circuit_broken = db.Column(db.Boolean, default=False, nullable=False)
    local_strategy_enabled = db.Column(db.Boolean, default=False, nullable=False)
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "api_key": self.api_key,
            "api_secret": self.api_secret[:6] + "..." if self.api_secret else "",
            "leverage": self.leverage,
            "balance_buffer_pct": self.balance_buffer_pct,
            "sizing_type": self.sizing_type,
            "fixed_amount": self.fixed_amount,
            "is_active": self.is_active,
            "daily_loss_limit": self.daily_loss_limit,
            "is_circuit_broken": self.is_circuit_broken,
            "local_strategy_enabled": self.local_strategy_enabled
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

class StrategyState(db.Model):
    __tablename__ = 'strategy_states'
    
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    symbol = db.Column(db.String(50), nullable=False)
    position_size = db.Column(db.Float, default=0.0, nullable=False) # 0 if flat, positive for long, negative for short
    entry_price = db.Column(db.Float, nullable=True)
    sl_dist = db.Column(db.Float, nullable=True)
    tp1_price = db.Column(db.Float, nullable=True)
    tp2_price = db.Column(db.Float, nullable=True)
    tp1_hit = db.Column(db.Boolean, default=False, nullable=False)
    tp2_hit = db.Column(db.Boolean, default=False, nullable=False)
    current_sl = db.Column(db.Float, nullable=True)
    last_signal_time = db.Column(db.Integer, nullable=True) # Unix timestamp of last processed closed candle
    updated_at = db.Column(db.DateTime, default=db.func.now(), onupdate=db.func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "position_size": self.position_size,
            "entry_price": self.entry_price,
            "sl_dist": self.sl_dist,
            "tp1_price": self.tp1_price,
            "tp2_price": self.tp2_price,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
            "current_sl": self.current_sl,
            "last_signal_time": self.last_signal_time,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }
