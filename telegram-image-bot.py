#!/usr/bin/env python3
"""
Telegram Inline Image Search Bot

A complete Telegram bot that provides inline image search using Google Custom Search JSON API.
Features:
- Inline mode support with @botname query
- Google Custom Search JSON API integration
- Debouncing to prevent excessive API calls
- TTL caching for query results
- Error handling and rate limiting
- Heroku deployment ready
- Safe search filters
- Pagination support

Requirements:
- python-telegram-bot>=20.0
- requests>=2.25.1
- python-dotenv>=0.19.0

Environment Variables:
- TELEGRAM_BOT_TOKEN: Your Telegram bot token from @BotFather
- GOOGLE_API_KEY: Google Custom Search API key
- GOOGLE_CSE_ID: Google Custom Search Engine ID
"""

import os
import json
import time
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Any
from functools import wraps
from datetime import datetime, timedelta
from uuid import uuid4

import requests
from dotenv import load_dotenv

from telegram import Update, InlineQueryResultPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, InlineQueryHandler, ContextTypes, CommandHandler
from telegram.error import TimedOut
from telegram.constants import ParseMode

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')

# Google Custom Search API configuration
GOOGLE_SEARCH_BASE_URL = "https://www.googleapis.com/customsearch/v1"
RESULTS_PER_PAGE = 5  # Show only top 5 images
CACHE_TTL = 300  # 5 minutes in seconds
DEBOUNCE_DELAY = 0.5  # 500ms debounce delay

if not all([TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, GOOGLE_CSE_ID]):
    raise ValueError("Missing required environment variables: TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, GOOGLE_CSE_ID")


class TTLCache:
    """
    Simple TTL (Time To Live) cache implementation for storing query results.
    """
    def __init__(self, ttl: int = CACHE_TTL):
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.ttl = ttl
    
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get value from cache if not expired."""
        if key in self.cache:
            item = self.cache[key]
            if time.time() - item['timestamp'] < self.ttl:
                return item['data']
            else:
                del self.cache[key]
        return None
    
    def set(self, key: str, value: Dict[str, Any]) -> None:
        """Set value in cache with timestamp."""
        self.cache[key] = {
            'data': value,
            'timestamp': time.time()
        }
    
    def clear_expired(self) -> None:
        """Clear expired items from cache."""
        current_time = time.time()
        expired_keys = [
            key for key, item in self.cache.items()
            if current_time - item['timestamp'] >= self.ttl
        ]
        for key in expired_keys:
            del self.cache[key]


class DebounceHandler:
    """
    Debounce handler to prevent excessive API calls during rapid typing.
    """
    def __init__(self, delay: float = DEBOUNCE_DELAY):
        self.delay = delay
        self.pending_tasks: Dict[str, asyncio.Task] = {}
    
    def debounce(self, func):
        """Decorator to debounce function calls."""
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Create unique key for this call
            key = f"{func.__name__}_{hash(str(args) + str(kwargs))}"
            
            # Cancel previous pending task if exists
            if key in self.pending_tasks:
                self.pending_tasks[key].cancel()
            
            # Create new task
            async def delayed_call():
                await asyncio.sleep(self.delay)
                return await func(*args, **kwargs)
            
            self.pending_tasks[key] = asyncio.create_task(delayed_call())
            
            try:
                result = await self.pending_tasks[key]
                return result
            except asyncio.CancelledError:
                # Task was cancelled, return None
                return None
            finally:
                # Clean up
                if key in self.pending_tasks:
                    del self.pending_tasks[key]
        
        return wrapper


class GoogleImageSearcher:
    """
    Google Custom Search JSON API client for image search.
    """
    def __init__(self, api_key: str, cse_id: str):
        self.api_key = api_key
        self.cse_id = cse_id
        self.cache = TTLCache()
        self.daily_quota_used = 0
        self.quota_reset_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    
    def _check_quota(self) -> bool:
        """Check if we haven't exceeded daily quota."""
        # Reset quota counter if it's a new day
        if datetime.now() >= self.quota_reset_time:
            self.daily_quota_used = 0
            self.quota_reset_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        
        return self.daily_quota_used < 100  # Free tier limit
    
    def _create_cache_key(self, query: str, start: int = 1) -> str:
        """Create cache key for query and pagination."""
        return f"search_{query}_{start}"
    
    async def search_images(self, query: str, start: int = 1) -> Dict[str, Any]:
        """
        Search for images using Google Custom Search API.
        
        Args:
            query: Search query string
            start: Starting result index for pagination
            
        Returns:
            Dictionary containing search results and metadata
        """
        # Check cache first
        cache_key = self._create_cache_key(query, start)
        cached_result = self.cache.get(cache_key)
        if cached_result:
            logger.info(f"Cache hit for query: {query}, start: {start}")
            return cached_result
        
        # Check quota
        if not self._check_quota():
            logger.warning("Daily quota exceeded")
            return {
                'error': 'Daily search limit reached (100 queries/day). Please try again tomorrow.',
                'items': []
            }
        
        # Prepare search parameters
        params = {
            'key': self.api_key,
            'cx': self.cse_id,
            'q': query,
            'searchType': 'image',
            'num': min(5, RESULTS_PER_PAGE),
            'start': start,
            'safe': 'active',
            'imgSize': 'large'
        }
        
        try:
            # Make API request
            response = requests.get(GOOGLE_SEARCH_BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            
            # Increment quota usage
            self.daily_quota_used += 1
            logger.info(f"API call successful. Quota used: {self.daily_quota_used}/100")
            
            result = response.json()
            
            # Process results
            processed_result = {
                'query': query,
                'start': start,
                'total_results': result.get('searchInformation', {}).get('totalResults', '0'),
                'items': []
            }
            
            # Extract image information
            for item in result.get('items', []):
                image_info = {
                    'title': item.get('title', ''),
                    'snippet': item.get('snippet', ''),
                    'link': item.get('link', ''),
                    'image_url': item.get('link', ''),
                    'thumbnail_url': item.get('image', {}).get('thumbnailLink', item.get('link', '')),
                    'width': item.get('image', {}).get('width', 0),
                    'height': item.get('image', {}).get('height', 0),
                    'context_link': item.get('image', {}).get('contextLink', ''),
                    'source_domain': item.get('displayLink', '')
                }
                processed_result['items'].append(image_info)
            
            # Cache the result
            self.cache.set(cache_key, processed_result)
            
            return processed_result
            
        except requests.RequestException as e:
            logger.error(f"Google API request failed: {e}")
            return {
                'error': f'Search service temporarily unavailable: {str(e)}',
                'items': []
            }
        except Exception as e:
            logger.error(f"Unexpected error during search: {e}")
            return {
                'error': 'An unexpected error occurred. Please try again.',
                'items': []
            }


class TelegramImageBot:
    """
    Main Telegram bot class for inline image search.
    """
    def __init__(self, token: str, google_api_key: str, google_cse_id: str):
        self.token = token
        self.searcher = GoogleImageSearcher(google_api_key, google_cse_id)
        self.debounce_handler = DebounceHandler()
        # Configure custom timeouts directly in the ApplicationBuilder
        self.application = (
            Application.builder()
            .token(token)
            .connect_timeout(15.0)
            .read_timeout(20.0)
            .write_timeout(20.0)
            .build()
        )
        
        # Apply debounce decorator to the handler method
        self._handle_inline_query = self.debounce_handler.debounce(self._handle_inline_query_impl)
        
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Setup bot handlers."""
        self.application.add_handler(CommandHandler("start", self._start_command))
        self.application.add_handler(CommandHandler("help", self._help_command))
        self.application.add_handler(InlineQueryHandler(self._handle_inline_query))
    
    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        start_message = (
            "🔍 **Image Search Bot**\n\n"
            "Use me in inline mode to search for images!\n\n"
            "**How to use:**\n"
            "1. Type `@{} <search query>` in any chat\n"
            "2. Wait for results to appear\n"
            "3. Tap on an image to send it\n\n"
            "**Examples:**\n"
            "• `@{} cat photos`\n"
            "• `@{} nature landscape`\n"
            "• `@{} technology gadgets`\n\n"
            "**Features:**\n"
            "✅ Safe search enabled\n"
            "✅ High-quality images\n"
            "✅ Fast results with caching\n"
            "✅ Pagination support\n\n"
            "**Note:** This bot uses Google Custom Search with a free quota of 100 searches per day."
        ).format(
            context.bot.username or "imagebot",
            context.bot.username or "imagebot", 
            context.bot.username or "imagebot",
            context.bot.username or "imagebot"
        )
        
        await update.message.reply_text(start_message, parse_mode=ParseMode.MARKDOWN)
    
    async def _help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        await self._start_command(update, context)

    def _parse_query(self, query: str) -> tuple[str, str]:
        """Parse the query to extract search term and caption.
        
        Args:
            query: The raw query string in format 'search_term | caption'
            
        Returns:
            tuple: (search_term, caption) - The search term and optional caption
        """
        try:
            # Split on the first occurrence of '|' to separate search term and caption
            parts = query.split('|', 1)
            if len(parts) == 2:
                search_term = parts[0].strip()
                caption = parts[1].strip()
                logger.info(f"Parsed query - Search: '{search_term}', Caption: '{caption}'")
                return search_term, caption
            return query.strip(), ""
        except Exception as e:
            logger.error(f"Error parsing query '{query}': {e}")
            return query.strip(), ""

    async def _handle_inline_query_impl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle inline queries with debouncing.
        This method is debounced to prevent excessive API calls during rapid typing.
        """
        logger.info(f"Received inline query: '{update.inline_query.query}' from user {update.inline_query.from_user.id}")
        
        # Parse query and caption
        query, caption = self._parse_query(update.inline_query.query.strip())

        # Handle empty query
        if not query:
            await update.inline_query.answer([], button=InlineKeyboardButton(
                text="Enter a search query to start...", switch_inline_query_current_chat=''
            ))
            return

        # Handle very short queries
        if len(query) < 3:
            await update.inline_query.answer([], button=InlineKeyboardButton(
                text="Query too short (min 3 characters)", switch_inline_query_current_chat=query
            ))
            return

        # Parse query for pagination
        start_index = 1
        if query.startswith("MORE:"):
            parts = query.split(":", 2)
            if len(parts) >= 3:
                try:
                    start_index = int(parts[1])
                    query = parts[2]
                    # Re-parse the query in case it contains a caption
                    query, caption = self._parse_query(query)
                except ValueError:
                    pass

        try:
            search_result = await self.searcher.search_images(query, start_index)

            if not search_result or not search_result.get('items'):
                await update.inline_query.answer([], button=InlineKeyboardButton(
                    text="No results found. Try another query.", switch_inline_query_current_chat=query
                ))
                return

            if 'error' in search_result:
                await update.inline_query.answer([], button=InlineKeyboardButton(
                    text=search_result['error'], switch_inline_query_current_chat=query
                ))
                return

            results = []
            for i, item in enumerate(search_result['items']):
                result_id = str(uuid4())
                title = item['title'][:100] if item['title'] else f"Image {i + 1}"
                description = item['snippet'][:100] if item['snippet'] else f"From {item['source_domain']}"
                
                try:
                    # Ensure caption is properly formatted
                    photo_caption = str(caption)[:1024] if caption else None
                    
                    # Create the result with the caption if provided
                    result = InlineQueryResultPhoto(
                        id=result_id,
                        photo_url=item['image_url'],
                        thumbnail_url=item['thumbnail_url'],
                        photo_width=800,
                        photo_height=600,
                        title=title[:63],
                        description=description[:255],
                        caption=photo_caption,
                        parse_mode=ParseMode.HTML if photo_caption and ('<' in photo_caption or '>' in photo_caption) else None
                    )
                    results.append(result)
                    
                    # Add a small delay between processing results
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.error(f"Error creating result for query '{query}': {e}")
                    # Fallback to result without caption if there's an error
                    results.append(InlineQueryResultPhoto(
                        id=result_id,
                        photo_url=item['image_url'],
                        thumbnail_url=item['thumbnail_url'],
                        photo_width=800,
                        photo_height=600,
                        title=title[:63],
                        description=description[:255]
                    ))

            next_offset = None
            if len(search_result['items']) == RESULTS_PER_PAGE:
                 next_offset=str(start_index + RESULTS_PER_PAGE)

            await update.inline_query.answer(
                results,
                cache_time=300,
                is_personal=True,
                next_offset=next_offset
            )

        except Exception as e:
            logger.error(f"Error handling inline query: {e}")
            await update.inline_query.answer([], button=InlineKeyboardButton(
                text="An error occurred. Please try again.", switch_inline_query_current_chat=query
            ))

    async def run(self):
        """Run the bot."""
        logger.info("Starting Telegram Image Search Bot...")
        
        # Start the bot
        await self.application.initialize()
        await self.application.start()
        await self.application.bot.set_my_commands([
            ("start", "Start the bot"),
            ("help", "Show help information")
        ])
        
        # Start polling for updates
        await self.application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        
        logger.info("Bot is running. Press Ctrl+C to stop.")
        
        # Keep the bot running
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Stopping bot...")
        finally:
            await self.application.updater.stop()
            await self.application.stop()
            logger.info("Bot stopped.")


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not Found')

def run_health_check():
    port = int(os.getenv('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logger.info(f"Health check server running on port {port}")
    
    # Start the server in a separate thread
    def run_server():
        server.serve_forever()
    
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    
    # Start keep-alive pings if running on Render
    if os.getenv('RENDER'):
        start_keepalive()
    
    return server

def start_keepalive():
    """Start a background thread to ping the health check endpoint."""
    def ping():
        import time
        import requests
        base_url = os.getenv('RENDER_EXTERNAL_URL') or 'http://localhost:8080'
        while True:
            try:
                requests.get(f"{base_url}/health", timeout=10)
                logger.info("Keep-alive ping sent")
            except Exception as e:
                logger.warning(f"Keep-alive ping failed: {e}")
            time.sleep(600)  # Ping every 10 minutes
    
    thread = threading.Thread(target=ping, daemon=True)
    thread.start()
    logger.info("Keep-alive thread started")
    return thread

async def main():
    """Main function to run the bot with retry logic."""
    # Start health check server
    run_health_check()
    
    while True:
        try:
            # Create and run the bot
            bot = TelegramImageBot(
                token=TELEGRAM_BOT_TOKEN,
                google_api_key=GOOGLE_API_KEY,
                google_cse_id=GOOGLE_CSE_ID
            )
            
            await bot.run()
        
        except TimedOut:
            logger.warning("Connection to Telegram timed out. Reconnecting in 15 seconds...")
            await asyncio.sleep(15)
        except Exception as e:
            logger.critical(f"An unexpected critical error occurred: {e}")
            logger.info("Bot will restart in 30 seconds.")
            await asyncio.sleep(30)


if __name__ == "__main__":
    # Run the bot
    asyncio.run(main())