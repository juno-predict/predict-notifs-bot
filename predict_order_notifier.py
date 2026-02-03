#!/usr/bin/env python3
"""
Predict.fun Order Fill Notification Bot for Telegram

This bot allows users to register their own wallet addresses and receive
real-time notifications when their limit orders are filled on predict.fun.

Commands:
  /start - Start the bot and see instructions
  /register <wallet_address> - Register your wallet address
  /status - Check your registration status
  /stop - Stop notifications and unregister

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
from typing import Optional, Dict
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
{emoji} <b>Order Filled on Predict.fun!</b>

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
            logger.error(f"Error formatting notification: {e}")
            return self.send_message(chat_id, f"⚠️ Order filled but error formatting: {e}")


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
        """Get order match events for a signer address"""
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

I'll send you real-time notifications when your limit orders are filled on predict.fun.

<b>How to use:</b>

1️⃣ <b>Find your Predict portfolio address:</b>
   • Go to <a href="https://predict.fun">predict.fun</a>
   • Click on your portfolio (top-right corner)
   • Copy the address shown below your username

2️⃣ <b>Register with the bot:</b>
   <code>/register 0xYourPortfolioAddress</code>

3️⃣ That's it! You'll receive notifications when orders fill.

<b>Other commands:</b>
• /status - Check your registration
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


def process_command(bot: TelegramBot, db: UserDatabase, message: dict):
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
                    time.sleep(0.5)  # Rate limit
                    
        except Exception as e:
            logger.error(f"Error checking orders for {chat_id}: {e}")
    
    def poll_orders(self):
        """Poll for order fills for all registered users"""
        while self.running:
            try:
                users = self.db.get_all_active_users()
                
                for chat_id, user_data in users.items():
                    if not self.running:
                        break
                    self.check_orders_for_user(chat_id, user_data)
                    time.sleep(1)  # Small delay between users
                
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
                        process_command(self.bot, self.db, message)
                        
            except Exception as e:
                logger.error(f"Error handling updates: {e}")
                time.sleep(5)
    
    def initialize_existing_users(self):
        """Mark existing orders as seen for all users (don't notify old fills)"""
        logger.info("Initializing existing users...")
        users = self.db.get_all_active_users()
        
        for chat_id, user_data in users.items():
            wallet = user_data.get('wallet_address')
            if not wallet:
                continue
            
            try:
                response = self.api.get_order_matches(wallet, first=50)
                if response.get('success'):
                    for match in response.get('data', []):
                        tx_hash = match.get('transactionHash')
                        if tx_hash:
                            self.db.add_seen_tx(chat_id, tx_hash)
                    logger.info(f"Initialized {chat_id}: marked existing orders as seen")
            except Exception as e:
                logger.error(f"Error initializing user {chat_id}: {e}")
            
            time.sleep(1)
    
    def run(self):
        """Start the bot"""
        print("""
╔═══════════════════════════════════════════════════════════════╗
║     Predict.fun Order Fill Notification Bot for Telegram      ║
║                     Multi-User Version                        ║
╠═══════════════════════════════════════════════════════════════╣
║  Users can register their own wallet addresses with:          ║
║  /register 0xWalletAddress                                    ║
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
