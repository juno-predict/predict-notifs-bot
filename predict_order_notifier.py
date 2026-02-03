#!/usr/bin/env python3
"""
Predict.fun Order Fill Notification Bot for Telegram

This bot monitors your predict.fun account for filled limit orders and sends
real-time notifications to your Telegram.

Setup:
1. Create a Telegram bot via @BotFather and get your bot token
2. Start a chat with your bot and get your chat_id
3. Get your predict.fun API key from https://predict.fun
4. Get your wallet address (signer address) from predict.fun
5. Configure the .env file with these credentials
6. Run the bot: python predict_order_notifier.py
"""

import os
import sys
import time
import json
import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
import requests
import websocket
import threading

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


@dataclass
class Config:
    """Configuration for the bot"""
    telegram_bot_token: str
    telegram_chat_id: str
    predict_api_key: str
    signer_address: str
    poll_interval: int = 10  # seconds between API polls
    use_websocket: bool = True  # Use WebSocket for real-time updates
    testnet: bool = False  # Use testnet instead of mainnet


def load_config() -> Config:
    """Load configuration from environment variables or .env file"""
    # Try to load from .env file
    env_file = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip().strip('"\'')
    
    # Required fields
    telegram_bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    predict_api_key = os.environ.get('PREDICT_API_KEY')
    signer_address = os.environ.get('SIGNER_ADDRESS')
    
    if not all([telegram_bot_token, telegram_chat_id, predict_api_key, signer_address]):
        logger.error("Missing required configuration. Please set:")
        logger.error("  TELEGRAM_BOT_TOKEN - Your Telegram bot token from @BotFather")
        logger.error("  TELEGRAM_CHAT_ID - Your Telegram chat ID")
        logger.error("  PREDICT_API_KEY - Your predict.fun API key")
        logger.error("  SIGNER_ADDRESS - Your wallet/signer address on predict.fun")
        sys.exit(1)
    
    return Config(
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        predict_api_key=predict_api_key,
        signer_address=signer_address,
        poll_interval=int(os.environ.get('POLL_INTERVAL', '10')),
        use_websocket=os.environ.get('USE_WEBSOCKET', 'true').lower() == 'true',
        testnet=os.environ.get('TESTNET', 'false').lower() == 'true'
    )


class TelegramNotifier:
    """Handles sending notifications to Telegram"""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
    
    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the configured Telegram chat"""
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info(f"Telegram message sent successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
    
    def send_order_fill_notification(self, fill_data: dict) -> bool:
        """Send a formatted order fill notification"""
        try:
            market = fill_data.get('market', {})
            taker = fill_data.get('taker', {})
            makers = fill_data.get('makers', [])
            
            market_title = market.get('title', 'Unknown Market')
            outcome_name = taker.get('outcome', {}).get('name', 'Unknown')
            quote_type = taker.get('quoteType', 'Unknown')
            amount_filled = fill_data.get('amountFilled', '0')
            price_executed = fill_data.get('priceExecuted', '0')
            tx_hash = fill_data.get('transactionHash', '')
            executed_at = fill_data.get('executedAt', '')
            
            # Convert amounts from wei to human readable
            try:
                amount_human = float(amount_filled) / 1e18
                price_human = float(price_executed) / 1e18
                value_usdt = amount_human * price_human
            except:
                amount_human = amount_filled
                price_human = price_executed
                value_usdt = 0
            
            # Determine if user is maker or taker
            signer = taker.get('signer', '').lower()
            is_taker = True
            role = "Taker"
            
            for maker in makers:
                if maker.get('signer', '').lower() == signer:
                    is_taker = False
                    role = "Maker"
                    break
            
            # Format the message
            emoji = "🟢" if quote_type == "Bid" else "🔴"
            action = "BUY" if quote_type == "Bid" else "SELL"
            
            message = f"""
{emoji} <b>Order Filled on Predict.fun!</b>

📊 <b>Market:</b> {market_title}
🎯 <b>Outcome:</b> {outcome_name}
💹 <b>Action:</b> {action}
👤 <b>Role:</b> {role}

📈 <b>Details:</b>
• Shares: {amount_human:.4f}
• Price: {price_human:.4f} USDT
• Value: ~{value_usdt:.2f} USDT

🔗 <a href="https://bscscan.com/tx/{tx_hash}">View Transaction</a>
⏰ {executed_at}
"""
            return self.send_message(message.strip())
            
        except Exception as e:
            logger.error(f"Error formatting order fill notification: {e}")
            return self.send_message(f"⚠️ Order filled but error formatting details: {str(e)}")


class PredictAPIClient:
    """Client for interacting with predict.fun API"""
    
    def __init__(self, api_key: str, testnet: bool = False):
        self.api_key = api_key
        if testnet:
            self.base_url = "https://api-testnet.predict.fun"
            self.ws_url = "wss://ws-testnet.predict.fun/ws"
        else:
            self.base_url = "https://api.predict.fun"
            self.ws_url = "wss://ws.predict.fun/ws"
        
        self.headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json"
        }
    
    def get_order_matches(
        self, 
        signer_address: str,
        first: int = 20,
        after: Optional[str] = None,
        is_signer_maker: Optional[bool] = None
    ) -> dict:
        """
        Get order match events for a specific signer address
        
        Args:
            signer_address: The wallet address to query
            first: Number of results to return
            after: Cursor for pagination
            is_signer_maker: Filter for maker (true) or taker (false) orders
        """
        url = f"{self.base_url}/v1/orders/matches"
        params = {
            "signerAddress": signer_address,
            "first": str(first)
        }
        
        if after:
            params["after"] = after
        
        if is_signer_maker is not None:
            params["isSignerMaker"] = str(is_signer_maker).lower()
        
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error fetching order matches: {e}")
            logger.error(f"Response: {e.response.text if e.response else 'No response'}")
            raise
        except Exception as e:
            logger.error(f"Error fetching order matches: {e}")
            raise
    
    def get_orders(self, status: str = "OPEN") -> dict:
        """Get orders for the authenticated user"""
        url = f"{self.base_url}/v1/orders"
        params = {"status": status}
        
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching orders: {e}")
            raise


class OrderFillMonitor:
    """Monitors for order fills using polling"""
    
    def __init__(
        self, 
        api_client: PredictAPIClient,
        notifier: TelegramNotifier,
        signer_address: str,
        poll_interval: int = 10
    ):
        self.api_client = api_client
        self.notifier = notifier
        self.signer_address = signer_address
        self.poll_interval = poll_interval
        self.seen_tx_hashes: set = set()
        self.running = False
        self.last_check_time: Optional[str] = None
    
    def _load_seen_hashes(self):
        """Load previously seen transaction hashes from file"""
        cache_file = os.path.join(os.path.dirname(__file__), '.seen_hashes.json')
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                    self.seen_tx_hashes = set(data.get('hashes', []))
                    self.last_check_time = data.get('last_check_time')
                    logger.info(f"Loaded {len(self.seen_tx_hashes)} seen transaction hashes")
            except Exception as e:
                logger.warning(f"Could not load seen hashes: {e}")
    
    def _save_seen_hashes(self):
        """Save seen transaction hashes to file"""
        cache_file = os.path.join(os.path.dirname(__file__), '.seen_hashes.json')
        try:
            # Only keep the last 1000 hashes
            hashes_list = list(self.seen_tx_hashes)[-1000:]
            with open(cache_file, 'w') as f:
                json.dump({
                    'hashes': hashes_list,
                    'last_check_time': self.last_check_time
                }, f)
        except Exception as e:
            logger.warning(f"Could not save seen hashes: {e}")
    
    def check_for_fills(self) -> list:
        """Check for new order fills"""
        try:
            response = self.api_client.get_order_matches(
                signer_address=self.signer_address,
                first=50
            )
            
            if not response.get('success'):
                logger.warning(f"API returned unsuccessful response: {response}")
                return []
            
            new_fills = []
            matches = response.get('data', [])
            
            for match in matches:
                tx_hash = match.get('transactionHash')
                if tx_hash and tx_hash not in self.seen_tx_hashes:
                    new_fills.append(match)
                    self.seen_tx_hashes.add(tx_hash)
            
            if new_fills:
                self.last_check_time = datetime.now(timezone.utc).isoformat()
                self._save_seen_hashes()
                logger.info(f"Found {len(new_fills)} new order fills")
            
            return new_fills
            
        except Exception as e:
            logger.error(f"Error checking for fills: {e}")
            return []
    
    def start_polling(self):
        """Start the polling loop"""
        self.running = True
        self._load_seen_hashes()
        
        # On first run, mark existing fills as seen (don't notify for old fills)
        logger.info("Initializing - checking for existing fills...")
        try:
            response = self.api_client.get_order_matches(
                signer_address=self.signer_address,
                first=100
            )
            if response.get('success'):
                for match in response.get('data', []):
                    tx_hash = match.get('transactionHash')
                    if tx_hash:
                        self.seen_tx_hashes.add(tx_hash)
                self._save_seen_hashes()
                logger.info(f"Marked {len(self.seen_tx_hashes)} existing fills as seen")
        except Exception as e:
            logger.warning(f"Could not fetch initial fills: {e}")
        
        self.notifier.send_message(
            "🚀 <b>Predict.fun Order Notifier Started!</b>\n\n"
            f"Monitoring address: <code>{self.signer_address[:10]}...{self.signer_address[-8:]}</code>\n"
            f"Polling interval: {self.poll_interval} seconds\n\n"
            "You'll receive notifications when your limit orders are filled."
        )
        
        logger.info(f"Starting polling loop (interval: {self.poll_interval}s)")
        
        while self.running:
            try:
                new_fills = self.check_for_fills()
                
                for fill in new_fills:
                    self.notifier.send_order_fill_notification(fill)
                    time.sleep(0.5)  # Small delay between notifications
                
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
            
            time.sleep(self.poll_interval)
    
    def stop(self):
        """Stop the polling loop"""
        self.running = False
        self._save_seen_hashes()
        logger.info("Polling stopped")


class WebSocketMonitor:
    """
    Monitors for order fills using WebSocket connection
    Note: This requires JWT authentication for private streams
    """
    
    def __init__(
        self,
        api_client: PredictAPIClient,
        notifier: TelegramNotifier,
        signer_address: str
    ):
        self.api_client = api_client
        self.notifier = notifier
        self.signer_address = signer_address
        self.ws: Optional[websocket.WebSocketApp] = None
        self.running = False
        self.request_id = 0
        self.heartbeat_timestamp = None
    
    def _get_next_request_id(self) -> int:
        self.request_id += 1
        return self.request_id
    
    def _on_message(self, ws, message):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(message)
            msg_type = data.get('type')
            topic = data.get('topic')
            
            # Handle heartbeat
            if msg_type == 'M' and topic == 'heartbeat':
                timestamp = data.get('data', {}).get('timestamp')
                if timestamp:
                    self.heartbeat_timestamp = timestamp
                    # Send heartbeat response
                    ws.send(json.dumps({
                        "method": "heartbeat",
                        "timestamp": timestamp
                    }))
                return
            
            # Handle subscription response
            if msg_type == 'R':
                logger.info(f"Subscription response: {data}")
                return
            
            # Handle order events
            if topic and 'order' in topic.lower():
                logger.info(f"Order event received: {data}")
                # Process order fill event
                event_data = data.get('data', {})
                if event_data.get('status') == 'FILLED':
                    self.notifier.send_message(
                        f"📬 <b>WebSocket Order Event</b>\n\n"
                        f"<pre>{json.dumps(event_data, indent=2)[:1000]}</pre>"
                    )
            
        except Exception as e:
            logger.error(f"Error processing WebSocket message: {e}")
    
    def _on_error(self, ws, error):
        """Handle WebSocket errors"""
        logger.error(f"WebSocket error: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket connection close"""
        logger.warning(f"WebSocket closed: {close_status_code} - {close_msg}")
        if self.running:
            logger.info("Attempting to reconnect in 5 seconds...")
            time.sleep(5)
            self._connect()
    
    def _on_open(self, ws):
        """Handle WebSocket connection open"""
        logger.info("WebSocket connected")
        
        # Note: Private streams like predictWalletEvents require JWT authentication
        # For now, we can subscribe to public market streams
        # To get private streams, you would need to:
        # 1. Get auth message from /v1/auth/message
        # 2. Sign it with your wallet
        # 3. Get JWT from /v1/auth/jwt
        # 4. Include JWT in subscription topic
        
        # Subscribe to public orderbook updates (as an example)
        # Private user order events would require: topic: f"predictWalletEvents:{jwt_token}"
        subscribe_msg = {
            "method": "subscribe",
            "requestId": self._get_next_request_id(),
            "topic": "orderbook:*"  # Subscribe to all orderbook updates
        }
        ws.send(json.dumps(subscribe_msg))
    
    def _connect(self):
        """Establish WebSocket connection"""
        ws_url = f"{self.api_client.ws_url}?apiKey={self.api_client.api_key}"
        
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        
        # Run in a separate thread
        ws_thread = threading.Thread(target=self.ws.run_forever)
        ws_thread.daemon = True
        ws_thread.start()
    
    def start(self):
        """Start the WebSocket monitor"""
        self.running = True
        logger.info("Starting WebSocket monitor...")
        self._connect()
    
    def stop(self):
        """Stop the WebSocket monitor"""
        self.running = False
        if self.ws:
            self.ws.close()
        logger.info("WebSocket monitor stopped")


def main():
    """Main entry point"""
    print("""
╔═══════════════════════════════════════════════════════════════╗
║     Predict.fun Order Fill Notification Bot for Telegram      ║
╠═══════════════════════════════════════════════════════════════╣
║  Monitors your predict.fun account and sends Telegram         ║
║  notifications when your limit orders are filled.             ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # Load configuration
    config = load_config()
    
    logger.info(f"Loaded configuration for address: {config.signer_address[:10]}...{config.signer_address[-8:]}")
    logger.info(f"Using {'testnet' if config.testnet else 'mainnet'} API")
    
    # Initialize components
    notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    api_client = PredictAPIClient(config.predict_api_key, config.testnet)
    
    # Test Telegram connection
    logger.info("Testing Telegram connection...")
    if not notifier.send_message("🔧 <b>Connection Test</b>\n\nPredict.fun notifier is starting up..."):
        logger.error("Failed to send test message to Telegram. Check your bot token and chat ID.")
        sys.exit(1)
    
    # Test API connection
    logger.info("Testing predict.fun API connection...")
    try:
        response = api_client.get_order_matches(config.signer_address, first=1)
        if response.get('success'):
            logger.info("API connection successful!")
        else:
            logger.warning(f"API returned: {response}")
    except Exception as e:
        logger.error(f"Failed to connect to predict.fun API: {e}")
        notifier.send_message(f"⚠️ <b>API Connection Error</b>\n\n{str(e)}")
        sys.exit(1)
    
    # Start monitoring
    monitor = OrderFillMonitor(
        api_client=api_client,
        notifier=notifier,
        signer_address=config.signer_address,
        poll_interval=config.poll_interval
    )
    
    try:
        monitor.start_polling()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal...")
        monitor.stop()
        notifier.send_message("🛑 <b>Predict.fun Order Notifier Stopped</b>")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        notifier.send_message(f"💥 <b>Fatal Error</b>\n\n{str(e)}")
        raise


if __name__ == "__main__":
    main()
