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
