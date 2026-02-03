#!/usr/bin/env python3
"""
Predict.fun Order Fill Notification Bot for Telegram

This bot allows users to register their own wallet addresses and receive
real-time notifications for their predict.fun trading activity.

Commands:
  /start - Start the bot and see instructions
  /register <wallet_address> - Register your wallet address
  /status - Check your registration status
  /orders - View all your open limit orders
  /stop - Stop notifications and unregister

Notifications:
  - Order placed
  - Order filled  
  - Price within 10% of your limit order

Setup (for bot operator):
1. Create a Telegram bot via @BotFather and get your bot token
2. Get your predict.fun API key from https://predict.fun
3. Set environment variables: TELEGRAM_BOT_TOKEN, PREDICT_API_KEY
4. Run the bot: python predict_order_notifier.py
"""

import os
import sys
import time
import json
import re
import asyncio
import logging
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List, Set
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('order_notifier.log')
    ]
)
logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    """Configuration for the bot"""
    telegram_bot_token: str
    predict_api_key: str
    poll_interval: int = 10
    testnet: bool = False


def load_config() -> Config:
    """Load configuration from environment variables"""
    telegram_bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    predict_api_key = os.environ.get('PREDICT_API_KEY')
    
    if not telegram_bot_token:
        logger.error("Missing TELEGRAM_BOT_TOKEN environment variable")
        sys.exit(1)
    
    if not predict_api_key:
        logger.error("Missing PREDICT_API_KEY environment variable")
        sys.exit(1)
    
    return Config(
        telegram_bot_token=telegram_bot_token,
        predict_api_key=predict_api_key,
        poll_interval=int(os.environ.get('POLL_INTERVAL', '10')),
        testnet=os.environ.get('TESTNET', 'false').lower() == 'true'
    )


# =============================================================================
# User Database (JSON file-based)
# =============================================================================

class UserDatabase:
    """Simple JSON file-based database for user registrations"""
    
    def __init__(self, filepath: str = "users.json"):
        self.filepath = filepath
        self.users: Dict[str, dict] = {}  # chat_id -> user_data
        self._load()
    
    def _load(self):
        """Load users from file"""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    self.users = json.load(f)
                logger.info(f"Loaded {len(self.users)} registered users")
            except Exception as e:
                logger.error(f"Error loading user database: {e}")
                self.users = {}
        else:
            self.users = {}
    
    def _save(self):
        """Save users to file"""
        try:
            with open(self.filepath, 'w') as f:
                json.dump(self.users, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving user database: {e}")
    
    def register_user(self, chat_id: str, wallet_address: str, username: str = None) -> bool:
        """Register a user with their wallet address"""
        self.users[chat_id] = {
            "wallet_address": wallet_address.lower(),
            "username": username,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "seen_tx_hashes": [],
            "seen_order_hashes": [],  # Track placed orders we've notified about
            "price_alerts_sent": {},  # order_hash -> last_alert_time (to avoid spam)
            "active": True
        }
        self._save()
        logger.info(f"Registered user {chat_id} with wallet {wallet_address[:10]}...")
        return True
    
    def unregister_user(self, chat_id: str) -> bool:
        """Unregister a user"""
        if chat_id in self.users:
            del self.users[chat_id]
            self._save()
            logger.info(f"Unregistered user {chat_id}")
            return True
        return False
    
    def get_user(self, chat_id: str) -> Optional[dict]:
        """Get user data by chat_id"""
        return self.users.get(chat_id)
    
    def get_all_active_users(self) -> Dict[str, dict]:
        """Get all active users"""
        return {cid: data for cid, data in self.users.items() if data.get('active', True)}
    
    def add_seen_tx(self, chat_id: str, tx_hash: str):
        """Add a transaction hash to user's seen list"""
        if chat_id in self.users:
            seen = self.users[chat_id].get('seen_tx_hashes', [])
            if tx_hash not in seen:
                seen.append(tx_hash)
                # Keep only last 500 hashes per user
                self.users[chat_id]['seen_tx_hashes'] = seen[-500:]
                self._save()
    
    def has_seen_tx(self, chat_id: str, tx_hash: str) -> bool:
        """Check if user has already seen this transaction"""
        if chat_id in self.users:
            return tx_hash in self.users[chat_id].get('seen_tx_hashes', [])
        return False
    
    def add_seen_order(self, chat_id: str, order_hash: str):
        """Add an order hash to user's seen placed orders list"""
        if chat_id in self.users:
            seen = self.users[chat_id].get('seen_order_hashes', [])
            if order_hash not in seen:
                seen.append(order_hash)
                self.users[chat_id]['seen_order_hashes'] = seen[-500:]
                self._save()
    
    def has_seen_order(self, chat_id: str, order_hash: str) -> bool:
        """Check if user has already been notified about this order placement"""
        if chat_id in self.users:
            return order_hash in self.users[chat_id].get('seen_order_hashes', [])
        return False
    
    def can_send_price_alert(self, chat_id: str, order_hash: str, cooldown_seconds: int = 3600) -> bool:
        """Check if we can send a price alert (with cooldown to avoid spam)"""
        if chat_id not in self.users:
            return False
        
        alerts = self.users[chat_id].get('price_alerts_sent', {})
        last_alert = alerts.get(order_hash)
        
        if last_alert is None:
            return True
        
        try:
            last_time = datetime.fromisoformat(last_alert)
            now = datetime.now(timezone.utc)
            return (now - last_time).total_seconds() > cooldown_seconds
        except:
            return True
    
    def record_price_alert(self, chat_id: str, order_hash: str):
        """Record that we sent a price alert for an order"""
        if chat_id in self.users:
            if 'price_alerts_sent' not in self.users[chat_id]:
                self.users[chat_id]['price_alerts_sent'] = {}
            self.users[chat_id]['price_alerts_sent'][order_hash] = datetime.now(timezone.utc).isoformat()
            # Clean up old alerts (keep last 100)
            alerts = self.users[chat_id]['price_alerts_sent']
            if len(alerts) > 100:
                sorted_alerts = sorted(alerts.items(), key=lambda x: x[1], reverse=True)[:100]
                self.users[chat_id]['price_alerts_sent'] = dict(sorted_alerts)
            self._save()


# =============================================================================
# Telegram Bot
# =============================================================================

class TelegramBot:
    """Handles Telegram bot interactions"""
    
    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.last_update_id = 0
    
    def send_message(self, chat_id: str, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to a chat"""
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return False
    
    def get_updates(self, timeout: int = 30) -> list:
        """Get new messages/commands from users"""
        try:
            url = f"{self.base_url}/getUpdates"
            params = {
                "offset": self.last_update_id + 1,
                "timeout": timeout,
                "allowed_updates": ["message"]
            }
            response = requests.get(url, params=params, timeout=timeout + 10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("ok") and data.get("result"):
                updates = data["result"]
                if updates:
                    self.last_update_id = updates[-1]["update_id"]
                return updates
            return []
        except Exception as e:
            logger.error(f"Error getting updates: {e}")
            return []
    
    def send_order_fill_notification(self, chat_id: str, fill_data: dict) -> bool:
        """Send a formatted order fill notification"""
        try:
            market = fill_data.get('market', {})
            taker = fill_data.get('taker', {})
            
            market_title = market.get('title', 'Unknown Market')
            outcome_name = taker.get('outcome', {}).get('name', 'Unknown')
            quote_type = taker.get('quoteType', 'Unknown')
            amount_filled = fill_data.get('amountFilled', '0')
            price_executed = fill_data.get('priceExecuted', '0')
            tx_hash = fill_data.get('transactionHash', '')
            executed_at = fill_data.get('executedAt', '')
            
            # Convert amounts from wei
            try:
                amount_human = float(amount_filled) / 1e18
                price_human = float(price_executed) / 1e18
                value_usdt = amount_human * price_human
            except:
                amount_human = amount_filled
                price_human = price_executed
                value_usdt = 0
            
            emoji = "🟢" if quote_type == "Bid" else "🔴"
            action = "BUY" if quote_type == "Bid" else "SELL"
            
            message = f"""
{emoji} <b>Order Filled!</b>

📊 <b>Market:</b> {market_title}
🎯 <b>Outcome:</b> {outcome_name}
💹 <b>Action:</b> {action}

📈 <b>Details:</b>
• Shares: {amount_human:.4f}
• Price: {price_human:.4f} USDT
• Value: ~{value_usdt:.2f} USDT

🔗 <a href="https://bscscan.com/tx/{tx_hash}">View Transaction</a>
⏰ {executed_at}
"""
            return self.send_message(chat_id, message.strip())
            
        except Exception as e:
            logger.error(f"Error formatting fill notification: {e}")
            return self.send_message(chat_id, f"⚠️ Order filled but error formatting: {e}")
    
    def send_order_placed_notification(self, chat_id: str, order_data: dict, market_data: dict = None) -> bool:
        """Send a notification when a new order is placed"""
        try:
            order = order_data.get('order', {})
            market_id = order_data.get('marketId', 'Unknown')
            amount = order_data.get('amount', '0')
            strategy = order_data.get('strategy', 'LIMIT')
            side = order.get('side', 0)
            
            # Get market title if available
            market_title = "Unknown Market"
            if market_data and market_data.get('success'):
                market_title = market_data.get('data', {}).get('title', 'Unknown Market')
            
            # Calculate price from maker/taker amounts
            try:
                maker_amount = int(order.get('makerAmount', 0))
                taker_amount = int(order.get('takerAmount', 0))
                amount_human = float(amount) / 1e18
                
                if side == 0:  # BUY - makerAmount is USDT, takerAmount is shares
                    price_human = maker_amount / taker_amount if taker_amount > 0 else 0
                else:  # SELL - makerAmount is shares, takerAmount is USDT
                    price_human = taker_amount / maker_amount if maker_amount > 0 else 0
                
                value_usdt = amount_human * price_human
            except:
                amount_human = 0
                price_human = 0
                value_usdt = 0
            
            action = "BUY" if side == 0 else "SELL"
            emoji = "📝"
            
            message = f"""
{emoji} <b>Order Placed!</b>

📊 <b>Market:</b> {market_title}
💹 <b>Action:</b> {action} ({strategy})

📈 <b>Details:</b>
• Shares: {amount_human:.4f}
• Price: {price_human:.4f} USDT
• Value: ~{value_usdt:.2f} USDT
"""
            return self.send_message(chat_id, message.strip())
            
        except Exception as e:
            logger.error(f"Error formatting order placed notification: {e}")
            return False
    
    def send_price_alert_notification(self, chat_id: str, order_data: dict, market_data: dict, 
                                       current_price: float, order_price: float, distance_pct: float) -> bool:
        """Send a notification when market price is within 10% of limit order"""
        try:
            order = order_data.get('order', {})
            side = order.get('side', 0)
            amount = order_data.get('amount', '0')
            
            market_title = "Unknown Market"
            if market_data and market_data.get('success'):
                market_title = market_data.get('data', {}).get('title', 'Unknown Market')
            
            try:
                amount_human = float(amount) / 1e18
            except:
                amount_human = 0
            
            action = "BUY" if side == 0 else "SELL"
            
            # Determine direction
            if side == 0:  # BUY order
                direction = "dropped" if current_price < order_price else "approaching"
            else:  # SELL order  
                direction = "risen" if current_price > order_price else "approaching"
            
            message = f"""
🔔 <b>Price Alert!</b>

Market price has {direction} to within <b>{distance_pct:.1f}%</b> of your limit order!

📊 <b>Market:</b> {market_title}
💹 <b>Your {action} Order:</b>
• Order Price: {order_price:.4f} USDT
• Current Price: {current_price:.4f} USDT
• Shares: {amount_human:.4f}

Your order may fill soon! 🎯
"""
            return self.send_message(chat_id, message.strip())
            
        except Exception as e:
            logger.error(f"Error formatting price alert: {e}")
            return False


# =============================================================================
# Predict.fun API Client
# =============================================================================

class PredictAPIClient:
    """Client for predict.fun API"""
    
    def __init__(self, api_key: str, testnet: bool = False):
        self.api_key = api_key
        self.base_url = "https://api-testnet.predict.fun" if testnet else "https://api.predict.fun"
        self.headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json"
        }
    
    def get_order_matches(self, signer_address: str, first: int = 20) -> dict:
        """Get order match events (filled orders) for a signer address"""
        url = f"{self.base_url}/v1/orders/matches"
        params = {
            "signerAddress": signer_address,
            "first": str(first)
        }
        
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching order matches for {signer_address[:10]}...: {e}")
            return {"success": False, "data": []}
    
    def get_open_orders(self, signer_address: str, first: int = 50) -> dict:
        """Get open orders for a signer address"""
        url = f"{self.base_url}/v1/orders"
        params = {
            "signerAddress": signer_address,
            "status": "OPEN",
            "first": str(first)
        }
        
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching open orders for {signer_address[:10]}...: {e}")
            return {"success": False, "data": []}
    
    def get_market(self, market_id: int) -> dict:
        """Get market details including current prices"""
        url = f"{self.base_url}/v1/markets/{market_id}"
        
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching market {market_id}: {e}")
            return {"success": False, "data": None}
    
    def get_orderbook(self, market_id: int) -> dict:
        """Get orderbook for a market"""
        url = f"{self.base_url}/v1/markets/{market_id}/orderbook"
        
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching orderbook for market {market_id}: {e}")
            return {"success": False, "data": None}


# =============================================================================
# Command Handlers
# =============================================================================

def is_valid_eth_address(address: str) -> bool:
    """Validate Ethereum address format"""
    return bool(re.match(r'^0x[a-fA-F0-9]{40}$', address))


def handle_start(bot: TelegramBot, chat_id: str, username: str):
    """Handle /start command"""
    message = """
👋 <b>Welcome to Predict.fun Order Notifier!</b>

I'll send you real-time notifications for your predict.fun trading activity.

<b>How to use:</b>

1️⃣ <b>Find your Predict portfolio address:</b>
   • Go to <a href="https://predict.fun">predict.fun</a>
   • Click on your portfolio (top-right corner)
   • Copy the address shown below your username

2️⃣ <b>Register with the bot:</b>
   <code>/register 0xYourPortfolioAddress</code>

3️⃣ That's it! You'll receive notifications for:
   • 📝 New orders placed
   • ✅ Orders filled
   • 🔔 Price within 10% of your limit orders

<b>Commands:</b>
• /status - Check your registration
• /orders - View your open orders
• /stop - Unregister and stop notifications
• /help - Show this message
"""
    bot.send_message(chat_id, message.strip())


def handle_register(bot: TelegramBot, db: UserDatabase, chat_id: str, username: str, args: str):
    """Handle /register command"""
    if not args:
        bot.send_message(chat_id, 
            "❌ Please provide your Predict portfolio address.\n\n"
            "<b>Usage:</b> <code>/register 0xYourPortfolioAddress</code>\n\n"
            "<b>How to find it:</b>\n"
            "1. Go to predict.fun\n"
            "2. Click your portfolio (top-right)\n"
            "3. Copy the address below your username"
        )
        return
    
    wallet_address = args.strip()
    
    if not is_valid_eth_address(wallet_address):
        bot.send_message(chat_id,
            "❌ Invalid address format.\n\n"
            "The address should:\n"
            "• Start with <code>0x</code>\n"
            "• Be followed by 40 hexadecimal characters\n\n"
            "<b>Example:</b> <code>0x1234567890abcdef1234567890abcdef12345678</code>"
        )
        return
    
    # Register the user
    db.register_user(chat_id, wallet_address, username)
    
    short_addr = f"{wallet_address[:6]}...{wallet_address[-4:]}"
    bot.send_message(chat_id,
        f"✅ <b>Successfully registered!</b>\n\n"
        f"📍 Portfolio: <code>{short_addr}</code>\n\n"
        f"You'll now receive notifications when your limit orders are filled on predict.fun.\n\n"
        f"Use /stop to unregister."
    )


def handle_status(bot: TelegramBot, db: UserDatabase, chat_id: str):
    """Handle /status command"""
    user = db.get_user(chat_id)
    
    if not user:
        bot.send_message(chat_id,
            "❌ You're not registered yet.\n\n"
            "Use <code>/register 0xYourPortfolioAddress</code> to start receiving notifications."
        )
        return
    
    wallet = user.get('wallet_address', 'Unknown')
    short_addr = f"{wallet[:6]}...{wallet[-4:]}"
    registered_at = user.get('registered_at', 'Unknown')
    seen_count = len(user.get('seen_tx_hashes', []))
    
    bot.send_message(chat_id,
        f"📊 <b>Your Status</b>\n\n"
        f"📍 Portfolio: <code>{short_addr}</code>\n"
        f"📅 Registered: {registered_at[:10]}\n"
        f"📬 Orders tracked: {seen_count}\n"
        f"✅ Status: Active\n\n"
        f"Use /stop to unregister."
    )


def handle_stop(bot: TelegramBot, db: UserDatabase, chat_id: str):
    """Handle /stop command"""
    user = db.get_user(chat_id)
    
    if not user:
        bot.send_message(chat_id, "ℹ️ You're not registered.")
        return
    
    db.unregister_user(chat_id)
    bot.send_message(chat_id,
        "👋 <b>Unregistered successfully!</b>\n\n"
        "You won't receive any more notifications.\n\n"
        "Use /register to sign up again anytime."
    )


def handle_help(bot: TelegramBot, chat_id: str):
    """Handle /help command"""
    handle_start(bot, chat_id, None)


def handle_orders(bot: TelegramBot, api: PredictAPIClient, db: UserDatabase, chat_id: str):
    """Handle /orders command - show all open orders"""
    user = db.get_user(chat_id)
    
    if not user:
        bot.send_message(chat_id,
            "❌ You're not registered yet.\n\n"
            "Use <code>/register 0xYourPortfolioAddress</code> to start."
        )
        return
    
    wallet = user.get('wallet_address')
    bot.send_message(chat_id, "🔍 Fetching your open orders...")
    
    try:
        response = api.get_open_orders(wallet)
        
        if not response.get('success'):
            bot.send_message(chat_id, "⚠️ Error fetching orders. Please try again.")
            return
        
        orders = response.get('data', [])
        
        if not orders:
            bot.send_message(chat_id, "📭 You have no open orders.")
            return
        
        # Group orders by market
        market_cache = {}
        order_lines = []
        
        for order_data in orders[:20]:  # Limit to 20 orders
            order = order_data.get('order', {})
            market_id = order_data.get('marketId')
            amount = order_data.get('amount', '0')
            amount_filled = order_data.get('amountFilled', '0')
            side = order.get('side', 0)
            strategy = order_data.get('strategy', 'LIMIT')
            
            # Get market info
            if market_id not in market_cache:
                market_response = api.get_market(market_id)
                if market_response.get('success'):
                    market_cache[market_id] = market_response.get('data', {})
                else:
                    market_cache[market_id] = {}
            
            market = market_cache.get(market_id, {})
            market_title = market.get('title', f'Market {market_id}')[:35]
            
            # Calculate price
            try:
                maker_amount = int(order.get('makerAmount', 0))
                taker_amount = int(order.get('takerAmount', 0))
                amount_human = float(amount) / 1e18
                filled_human = float(amount_filled) / 1e18
                remaining = amount_human - filled_human
                
                if side == 0:  # BUY
                    price = maker_amount / taker_amount if taker_amount > 0 else 0
                else:  # SELL
                    price = taker_amount / maker_amount if maker_amount > 0 else 0
            except:
                price = 0
                remaining = 0
            
            action = "🟢 BUY" if side == 0 else "🔴 SELL"
            
            order_lines.append(
                f"{action} | {remaining:.2f} @ {price:.3f}\n"
                f"   <i>{market_title}...</i>"
            )
        
        message = f"📋 <b>Your Open Orders ({len(orders)})</b>\n\n"
        message += "\n\n".join(order_lines)
        
        if len(orders) > 20:
            message += f"\n\n<i>...and {len(orders) - 20} more</i>"
        
        bot.send_message(chat_id, message)
        
    except Exception as e:
        logger.error(f"Error in handle_orders: {e}")
        bot.send_message(chat_id, f"⚠️ Error fetching orders: {str(e)[:100]}")


def process_command(bot: TelegramBot, db: UserDatabase, api: PredictAPIClient, message: dict):
    """Process an incoming command"""
    chat = message.get('chat', {})
    chat_id = str(chat.get('id'))
    username = message.get('from', {}).get('username', '')
    text = message.get('text', '').strip()
    
    if not text.startswith('/'):
        return
    
    # Parse command and arguments
    parts = text.split(maxsplit=1)
    command = parts[0].lower().split('@')[0]  # Handle @botname suffix
    args = parts[1] if len(parts) > 1 else ''
    
    logger.info(f"Command from {chat_id}: {command}")
    
    if command == '/start':
        handle_start(bot, chat_id, username)
    elif command == '/register':
        handle_register(bot, db, chat_id, username, args)
    elif command == '/status':
        handle_status(bot, db, chat_id)
    elif command == '/orders':
        handle_orders(bot, api, db, chat_id)
    elif command == '/stop':
        handle_stop(bot, db, chat_id)
    elif command == '/help':
        handle_help(bot, chat_id)
    else:
        bot.send_message(chat_id, "❓ Unknown command. Use /help to see available commands.")


# =============================================================================
# Main Bot Loop
# =============================================================================

class OrderNotifierBot:
    """Main bot class that coordinates everything"""
    
    def __init__(self, config: Config):
        self.config = config
        self.bot = TelegramBot(config.telegram_bot_token)
        self.api = PredictAPIClient(config.predict_api_key, config.testnet)
        self.db = UserDatabase()
        self.running = False
        self.market_cache = {}  # Cache market data to reduce API calls
        self.market_cache_time = {}  # Track when market was cached
    
    def get_market_cached(self, market_id: int, max_age_seconds: int = 60) -> dict:
        """Get market data with caching"""
        now = time.time()
        
        if market_id in self.market_cache:
            cache_time = self.market_cache_time.get(market_id, 0)
            if now - cache_time < max_age_seconds:
                return self.market_cache[market_id]
        
        # Fetch fresh data
        response = self.api.get_market(market_id)
        if response.get('success'):
            self.market_cache[market_id] = response
            self.market_cache_time[market_id] = now
        
        return response
    
    def get_best_price(self, market_id: int, side: int) -> Optional[float]:
        """Get the best current price for a market side (0=BUY, 1=SELL)"""
        try:
            response = self.api.get_orderbook(market_id)
            if not response.get('success'):
                return None
            
            data = response.get('data', {})
            bids = data.get('bids', [])  # Buy orders
            asks = data.get('asks', [])  # Sell orders
            
            # For a BUY limit order, compare against best ask (lowest sell price)
            # For a SELL limit order, compare against best bid (highest buy price)
            if side == 0:  # BUY order - look at asks
                if asks and len(asks) > 0 and len(asks[0]) > 0:
                    return float(asks[0][0]) / 1e18 if asks[0][0] else None
            else:  # SELL order - look at bids
                if bids and len(bids) > 0 and len(bids[0]) > 0:
                    return float(bids[0][0]) / 1e18 if bids[0][0] else None
            
            return None
        except Exception as e:
            logger.error(f"Error getting best price for market {market_id}: {e}")
            return None
    
    def check_orders_for_user(self, chat_id: str, user_data: dict):
        """Check for new order fills for a specific user"""
        wallet = user_data.get('wallet_address')
        if not wallet:
            return
        
        try:
            response = self.api.get_order_matches(wallet)
            
            if not response.get('success'):
                return
            
            for match in response.get('data', []):
                tx_hash = match.get('transactionHash')
                if tx_hash and not self.db.has_seen_tx(chat_id, tx_hash):
                    # New order fill!
                    self.db.add_seen_tx(chat_id, tx_hash)
                    self.bot.send_order_fill_notification(chat_id, match)
                    logger.info(f"Notified {chat_id} of order fill: {tx_hash[:16]}...")
                    time.sleep(0.5)
                    
        except Exception as e:
            logger.error(f"Error checking order fills for {chat_id}: {e}")
    
    def check_new_orders_for_user(self, chat_id: str, user_data: dict):
        """Check for newly placed orders"""
        wallet = user_data.get('wallet_address')
        if not wallet:
            return
        
        try:
            response = self.api.get_open_orders(wallet)
            
            if not response.get('success'):
                return
            
            for order_data in response.get('data', []):
                order = order_data.get('order', {})
                order_hash = order.get('hash')
                
                if order_hash and not self.db.has_seen_order(chat_id, order_hash):
                    # New order placed!
                    self.db.add_seen_order(chat_id, order_hash)
                    
                    # Get market data for better notification
                    market_id = order_data.get('marketId')
                    market_data = self.get_market_cached(market_id) if market_id else None
                    
                    self.bot.send_order_placed_notification(chat_id, order_data, market_data)
                    logger.info(f"Notified {chat_id} of new order: {order_hash[:16]}...")
                    time.sleep(0.5)
                    
        except Exception as e:
            logger.error(f"Error checking new orders for {chat_id}: {e}")
    
    def check_price_alerts_for_user(self, chat_id: str, user_data: dict):
        """Check if market price is within 10% of user's limit orders"""
        wallet = user_data.get('wallet_address')
        if not wallet:
            return
        
        try:
            response = self.api.get_open_orders(wallet)
            
            if not response.get('success'):
                return
            
            for order_data in response.get('data', []):
                order = order_data.get('order', {})
                order_hash = order.get('hash')
                market_id = order_data.get('marketId')
                side = order.get('side', 0)
                strategy = order_data.get('strategy', '')
                
                # Only check LIMIT orders
                if strategy != 'LIMIT' or not market_id or not order_hash:
                    continue
                
                # Check cooldown (1 hour between alerts for same order)
                if not self.db.can_send_price_alert(chat_id, order_hash, cooldown_seconds=3600):
                    continue
                
                # Calculate order price
                try:
                    maker_amount = int(order.get('makerAmount', 0))
                    taker_amount = int(order.get('takerAmount', 0))
                    
                    if side == 0:  # BUY
                        order_price = maker_amount / taker_amount if taker_amount > 0 else 0
                    else:  # SELL
                        order_price = taker_amount / maker_amount if maker_amount > 0 else 0
                except:
                    continue
                
                if order_price <= 0:
                    continue
                
                # Get current market price
                current_price = self.get_best_price(market_id, side)
                
                if current_price is None or current_price <= 0:
                    continue
                
                # Calculate distance
                if side == 0:  # BUY order - alert if ask price drops close to our bid
                    distance_pct = ((current_price - order_price) / order_price) * 100
                    should_alert = 0 <= distance_pct <= 10  # Price is within 10% above our buy price
                else:  # SELL order - alert if bid price rises close to our ask
                    distance_pct = ((order_price - current_price) / order_price) * 100
                    should_alert = 0 <= distance_pct <= 10  # Price is within 10% below our sell price
                
                if should_alert:
                    market_data = self.get_market_cached(market_id)
                    self.bot.send_price_alert_notification(
                        chat_id, order_data, market_data,
                        current_price, order_price, abs(distance_pct)
                    )
                    self.db.record_price_alert(chat_id, order_hash)
                    logger.info(f"Sent price alert to {chat_id} for order {order_hash[:16]}...")
                    time.sleep(0.5)
                    
        except Exception as e:
            logger.error(f"Error checking price alerts for {chat_id}: {e}")
    
    def poll_orders(self):
        """Poll for order activity for all registered users"""
        while self.running:
            try:
                users = self.db.get_all_active_users()
                
                for chat_id, user_data in users.items():
                    if not self.running:
                        break
                    
                    # Check for filled orders
                    self.check_orders_for_user(chat_id, user_data)
                    time.sleep(0.5)
                    
                    # Check for new orders placed
                    self.check_new_orders_for_user(chat_id, user_data)
                    time.sleep(0.5)
                    
                    # Check price alerts (every other cycle to reduce API load)
                    self.check_price_alerts_for_user(chat_id, user_data)
                    time.sleep(0.5)
                
            except Exception as e:
                logger.error(f"Error in order polling loop: {e}")
            
            # Wait before next poll cycle
            for _ in range(self.config.poll_interval):
                if not self.running:
                    break
                time.sleep(1)
    
    def handle_updates(self):
        """Handle incoming Telegram messages/commands"""
        while self.running:
            try:
                updates = self.bot.get_updates(timeout=30)
                
                for update in updates:
                    message = update.get('message')
                    if message and message.get('text'):
                        process_command(self.bot, self.db, self.api, message)
                        
            except Exception as e:
                logger.error(f"Error handling updates: {e}")
                time.sleep(5)
    
    def initialize_existing_users(self):
        """Mark existing orders as seen for all users (don't notify old activity)"""
        logger.info("Initializing existing users...")
        users = self.db.get_all_active_users()
        
        for chat_id, user_data in users.items():
            wallet = user_data.get('wallet_address')
            if not wallet:
                continue
            
            try:
                # Mark existing filled orders as seen
                response = self.api.get_order_matches(wallet, first=50)
                if response.get('success'):
                    for match in response.get('data', []):
                        tx_hash = match.get('transactionHash')
                        if tx_hash:
                            self.db.add_seen_tx(chat_id, tx_hash)
                
                # Mark existing open orders as seen
                response = self.api.get_open_orders(wallet, first=50)
                if response.get('success'):
                    for order_data in response.get('data', []):
                        order_hash = order_data.get('order', {}).get('hash')
                        if order_hash:
                            self.db.add_seen_order(chat_id, order_hash)
                
                logger.info(f"Initialized {chat_id}: marked existing orders as seen")
            except Exception as e:
                logger.error(f"Error initializing user {chat_id}: {e}")
            
            time.sleep(1)
    
    def run(self):
        """Start the bot"""
        print("""
╔═══════════════════════════════════════════════════════════════╗
║     Predict.fun Order Notification Bot for Telegram           ║
║                     Multi-User Version                        ║
╠═══════════════════════════════════════════════════════════════╣
║  Notifications:                                               ║
║  • 📝 Order placed                                            ║
║  • ✅ Order filled                                            ║
║  • 🔔 Price within 10% of limit order                         ║
╚═══════════════════════════════════════════════════════════════╝
        """)
        
        logger.info(f"Using {'testnet' if self.config.testnet else 'mainnet'} API")
        logger.info(f"Poll interval: {self.config.poll_interval} seconds")
        
        # Initialize existing users
        self.initialize_existing_users()
        
        self.running = True
        
        # Start order polling in a separate thread
        poll_thread = threading.Thread(target=self.poll_orders, daemon=True)
        poll_thread.start()
        logger.info("Order polling started")
        
        # Handle Telegram updates in main thread
        logger.info("Bot is running! Waiting for commands...")
        
        try:
            self.handle_updates()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self.running = False
            poll_thread.join(timeout=5)
            logger.info("Bot stopped")


# =============================================================================
# Entry Point
# =============================================================================

def main():
    config = load_config()
    bot = OrderNotifierBot(config)
    bot.run()


if __name__ == "__main__":
    main()
