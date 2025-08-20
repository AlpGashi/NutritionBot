import os
import requests
import json
from difflib import get_close_matches
from notion_client import Client
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, ConversationHandler, filters
import re
from dotenv import load_dotenv
import http.server
import socketserver
import threading

# Load environment variables
load_dotenv()

# 🔐 Credentials from environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
MODEL = "openai/gpt-oss-20b:free"
DB_NAME = "Nutrition Tracker"
DAILY_LOG_DB_ID = "255d27f1ba33805eb52bc8e000494b64"

# Conversation states
FOOD_INPUT, SERVING_INPUT, BMR_INFO, AGE, GENDER, WEIGHT, HEIGHT, ACTIVITY, FREE_FORM_INPUT = range(9)

# User data storage
user_data = {}

# ✅ Initialize Notion client
notion = Client(auth=NOTION_TOKEN)

# 📦 Load fixed macro list
try:
    with open("food_reference.json", "r") as f:
        known_foods = json.load(f)
except FileNotFoundError:
    known_foods = {}

# Simple HTTP server for health checks
class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "healthy", "bot": "running"}')
        else:
            self.send_response(404)
            self.end_headers()

def run_health_check():
    PORT = 5000
    with socketserver.TCPServer(("", PORT), HealthCheckHandler) as httpd:
        print(f"Health check server running on port {PORT}")
        httpd.serve_forever()

# 🧮 BMR Calculation Functions
def calculate_bmr(gender, weight, height, age):
    if gender.lower() == 'male':
        return 66.47 + (13.75 * weight) + (5.003 * height) - (6.755 * age)
    else:
        return 655.1 + (9.563 * weight) + (1.85 * height) - (4.676 * age)

def calculate_total_calories(bmr, activity_level):
    activity_factors = {
        'sedentary': 1.2,
        'light': 1.375,
        'moderate': 1.55,
        'active': 1.725,
        'extra': 1.9
    }
    return bmr * activity_factors.get(activity_level.lower(), 1.2)

# 🤖 Nutrition Functions
def match_known_food(food_name, known_foods, cutoff=0.85):
    matches = get_close_matches(food_name.lower(), known_foods.keys(), n=1, cutoff=cutoff)
    return matches[0] if matches else None

def scale_macros(base_macros, serving_size):
    factor = serving_size / 100
    return {
        "Calories": round(base_macros["Calories"] * factor, 2),
        "Protein": round(base_macros["Protein"] * factor, 2),
        "Carbohydrates": round(base_macros["Carbohydrates"] * factor, 2),
        "Fats": round(base_macros["Fats"] * factor, 2)
    }

def get_macros_from_ai(food_name, serving_size):
    prompt = f"""Return only JSON with keys: Calories, Protein, Carbohydrates, Fats.
Food: {food_name}
Serving Size: {serving_size}g"""
    
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={"model": MODEL, "messages": [{"role": "user", "content": prompt}]}
        )
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except:
        return None

# 📝 Notion Functions
def get_nutrition_db_id():
    try:
        response = notion.search(filter={"value": "database", "property": "object"})
        nutrition_dbs = [
            result for result in response["results"]
            if result["object"] == "database" and result.get("title") and
            result["title"][0]["text"]["content"].lower() == DB_NAME.lower()
        ]
        return nutrition_dbs[0]["id"] if nutrition_dbs else None
    except:
        return None

def add_food_to_notion(food, serving, macros):
    try:
        db_id = get_nutrition_db_id()
        if not db_id:
            return False

        notion.pages.create(
            parent={"database_id": db_id},
            properties={
                "Food name": {"title": [{"text": {"content": food}}]},
                "Serving size (grams)": {"number": serving},
                "Calories": {"number": macros["Calories"]},
                "Protein": {"number": macros["Protein"]},
                "Carbohydrates": {"number": macros["Carbohydrates"]},
                "Fats": {"number": macros["Fats"]}
            }
        )
        return True
    except Exception as e:
        print(f"Error adding to Notion: {e}")
        return False

# 🧠 IMPROVED Natural Language Processing with Clean Food Names
def clean_food_name(food_name):
    """Clean up food names to remove numbers, units, and unnecessary words"""
    # Remove numbers and decimal points
    cleaned = re.sub(r'\d+\.?\d*\s*', '', food_name)
    # Remove common units and garbage words
    cleaned = re.sub(r'\b(pieces|piece|g|grams|gram|of|the|and|with)\b', '', cleaned, flags=re.IGNORECASE)
    # Remove extra spaces and trim
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Capitalize first letter of each word
    cleaned = ' '.join(word.capitalize() for word in cleaned.split())
    return cleaned

def parse_food_text(text):
    """Parse free-form text like '2 bananas 150g chicken breast'"""
    food_items = []
    
    # Standard serving sizes for common items
    standard_servings = {
        'banana': 120, 'bananas': 120,
        'apple': 182, 'apples': 182,
        'orange': 131, 'oranges': 131,
        'egg': 50, 'eggs': 50,
        'slice bread': 28, 'bread': 28,
        'tbsp oil': 14, 'tbsp olive oil': 14, 'tablespoon oil': 14,
        'tsp oil': 5, 'teaspoon oil': 5,
        'chicken breast': 100, 'chicken': 100,
        'rice': 100, 'white rice': 100, 'brown rice': 100,
        'pasta': 100, 'spaghetti': 100,
        'ice cream': 66, 'vanilla ice cream': 66, 'icecream': 66,
        'milk': 240, 'water': 240,
        'coffee': 240, 'tea': 240
    }
    
    # Improved pattern matching with better capture groups
    potential_items = []
    
    # Pattern 1: Xg FOOD (e.g., "100g chicken breast")
    matches = re.finditer(r'(\d+)\s*g\s*([a-zA-Z][a-zA-Z\s]*)', text.lower())
    for match in matches:
        serving = float(match.group(1))
        food = clean_food_name(match.group(2))
        if food:  # Only add if we have a valid food name
            potential_items.append({'food': food, 'serving': serving, 'unit': 'g'})
    
    # Pattern 2: NUMBER FOOD (e.g., "2 bananas")
    matches = re.finditer(r'(\d+)\s+([a-zA-Z][a-zA-Z\s]*)', text.lower())
    for match in matches:
        quantity = float(match.group(1))
        food = clean_food_name(match.group(2))
        
        if food:
            # Look up standard serving size
            serving = standard_servings.get(food.lower(), 100) * quantity
            potential_items.append({'food': food, 'serving': serving, 'unit': 'pieces', 'quantity': quantity})
    
    # Pattern 3: UNIT FOOD (e.g., "2 tbsp olive oil")
    matches = re.finditer(r'(\d+)\s*(tbsp|tsp|tablespoon|teaspoon)\s+([a-zA-Z\s]+)', text.lower())
    for match in matches:
        quantity = float(match.group(1))
        unit = match.group(2)
        food = clean_food_name(match.group(3))
        
        if food:
            # Convert units to grams
            unit_multiplier = 14 if unit in ['tbsp', 'tablespoon'] else 5
            serving = quantity * unit_multiplier
            display_name = f"{food} ({unit})" if unit in ['tbsp', 'tsp'] else food
            potential_items.append({'food': display_name, 'serving': serving, 'unit': unit, 'quantity': quantity})
    
    # Filter out invalid items and duplicates
    seen = set()
    for item in potential_items:
        food_name = item['food'].strip()
        # Skip empty or very short names
        if len(food_name) < 2:
            continue
        
        # Remove duplicates
        if food_name not in seen:
            seen.add(food_name)
            food_items.append(item)
    
    return food_items

# 💬 Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ['🍎 Log Food', '📊 Calculate BMR'],
        ['📈 Today\'s Calories', '📝 Free Form Input']
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "🤖 Nutrition Bot Ready!\n\n"
        "• 🍎 Log Food - Add what you've eaten\n"
        "• 📝 Free Form Input - Write like '2 bananas 150g chicken'\n"
        "• 📊 Calculate BMR - Calculate your calorie needs\n"
        "• 📈 Today's Calories - Check today's total\n\n"
        "Try: '2 bananas 100g chicken breast 2 tbsp oil 100g vanilla ice cream'",
        reply_markup=reply_markup
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text == '🍎 Log Food':
        await update.message.reply_text("What did you eat? (e.g., 'Chicken breast')")
        return FOOD_INPUT
    elif text == '📊 Calculate BMR':
        await update.message.reply_text("Let's calculate your BMR! What's your age?")
        return AGE
    elif text == '📈 Today\'s Calories':
        await show_todays_calories(update, context)
    elif text == '📝 Free Form Input':
        await update.message.reply_text(
            "📝 Write what you ate in natural language:\n\n"
            "Examples:\n"
            "• '2 bananas 100g chicken breast'\n"
            "• '2 tbsp olive oil 100g vanilla ice cream'\n"
            "• '150g white rice with vegetables'"
        )
        return FREE_FORM_INPUT
    else:
        await update.message.reply_text("Please choose an option from the menu!")

async def free_form_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    food_items = parse_food_text(user_text)
    
    if not food_items:
        await update.message.reply_text("❌ Couldn't identify any food items. Try: '2 bananas 100g chicken'")
        return ConversationHandler.END
    
    total_calories = 0
    successful_items = []
    failed_items = []
    
    for item in food_items:
        food = item['food']
        serving = item['serving']
        
        # Get macros
        matched = match_known_food(food, known_foods)
        if matched:
            macros = scale_macros(known_foods[matched], serving)
            source = "database"
        else:
            macros = get_macros_from_ai(food, serving)
            source = "AI"
        
        if macros:
            # Add to Notion - use clean food name only
            success = add_food_to_notion(food, serving, macros)
            
            if success:
                successful_items.append({
                    'name': food,
                    'calories': macros['Calories'],
                    'protein': macros['Protein'],
                    'serving': serving
                })
                total_calories += macros['Calories']
            else:
                failed_items.append(food)
        else:
            failed_items.append(food)
    
    # Build response message
    response_message = "✅ Added to tracker:\n\n"
    
    for item in successful_items:
        response_message += f"🍎 {item['name']}\n"
        response_message += f"   Serving: {item['serving']}g | 🔥 {item['calories']} cal | 💪 {item['protein']}g\n"
    
    if failed_items:
        response_message += f"\n❌ Failed to add: {', '.join(failed_items)}"
    
    response_message += f"\n📊 Total: {total_calories:.0f} calories"
    await update.message.reply_text(response_message)
    return ConversationHandler.END

async def food_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['food'] = update.message.text
    await update.message.reply_text("How many grams? (e.g., 150)")
    return SERVING_INPUT

async def serving_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        serving = float(update.message.text)
        food = context.user_data['food']
        
        # Clean the food name
        clean_food = clean_food_name(food)
        
        # Get macros
        matched = match_known_food(clean_food, known_foods)
        if matched:
            macros = scale_macros(known_foods[matched], serving)
            source = "database"
        else:
            macros = get_macros_from_ai(clean_food, serving)
            source = "AI"
        
        if macros:
            # Add to Notion with clean food name
            success = add_food_to_notion(clean_food, serving, macros)
            
            if success:
                message = (f"✅ Added to tracker!\n"
                          f"🍎 {clean_food} ({serving}g)\n"
                          f"🔥 Calories: {macros['Calories']}\n"
                          f"💪 Protein: {macros['Protein']}g\n"
                          f"🍞 Carbs: {macros['Carbohydrates']}g\n"
                          f"🥑 Fats: {macros['Fats']}g\n"
                          f"(Source: {source})")
            else:
                message = "❌ Failed to add to Notion"
        else:
            message = "❌ Could not find nutrition data for this food"
        
        await update.message.reply_text(message)
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("Please enter a valid number for grams!")
        return SERVING_INPUT

async def age_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['age'] = int(update.message.text)
        keyboard = [['Male', 'Female']]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("What's your gender?", reply_markup=reply_markup)
        return GENDER
    except ValueError:
        await update.message.reply_text("Please enter a valid age!")
        return AGE

async def gender_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gender'] = update.message.text
    await update.message.reply_text("What's your weight in kg? (e.g., 70)")
    return WEIGHT

async def weight_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['weight'] = float(update.message.text)
        await update.message.reply_text("What's your height in cm? (e.g., 175)")
        return HEIGHT
    except ValueError:
        await update.message.reply_text("Please enter a valid weight!")
        return WEIGHT

async def height_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['height'] = float(update.message.text)
        
        keyboard = [
            ['Sedentary (little/no exercise)'],
            ['Light exercise (1-3 days/week)'],
            ['Moderate exercise (3-5 days/week)'],
            ['Active (6-7 days/week)'],
            ['Extra active (athlete level)']
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            "What's your activity level?",
            reply_markup=reply_markup
        )
        return ACTIVITY
    except ValueError:
        await update.message.reply_text("Please enter a valid height!")
        return HEIGHT

async def activity_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activity_map = {
        'sedentary': 'sedentary',
        'light': 'light',
        'moderate': 'moderate',
        'active': 'active',
        'extra': 'extra'
    }
    
    activity_text = update.message.text.lower()
    activity_level = 'sedentary'
    
    for key, value in activity_map.items():
        if key in activity_text:
            activity_level = value
            break
    
    # Calculate BMR
    bmr = calculate_bmr(
        context.user_data['gender'],
        context.user_data['weight'],
        context.user_data['height'],
        context.user_data['age']
    )
    
    total_calories = calculate_total_calories(bmr, activity_level)
    
    message = (f"📊 Your Results:\n\n"
              f"• BMR: {bmr:.0f} calories/day\n"
              f"• Activity: {activity_level.title()}\n"
              f"• Total Daily Need: {total_calories:.0f} calories\n\n"
              f"This is your estimated maintenance calories!")
    
    await update.message.reply_text(message)
    return ConversationHandler.END

async def show_todays_calories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Get nutrition database ID
        db_id = get_nutrition_db_id()
        if not db_id:
            await update.message.reply_text("❌ Nutrition database not found!")
            return
        
        # Get all entries from nutrition database
        entries = notion.databases.query(database_id=db_id)["results"]
        
        total_calories = 0
        calories_count = 0
        
        # Find the calories property name
        db_props = notion.databases.retrieve(database_id=db_id)["properties"]
        calories_prop = None
        
        for prop_name, prop_details in db_props.items():
            if 'calori' in prop_name.lower():
                calories_prop = prop_name
                break
        
        if not calories_prop:
            await update.message.reply_text("❌ Could not find Calories property in database")
            return
        
        # Calculate total calories from all entries
        for entry in entries:
            props = entry["properties"]
            calories = props.get(calories_prop, {}).get('number')
            if calories is not None:
                total_calories += calories
                calories_count += 1
        
        if calories_count > 0:
            await update.message.reply_text(
                f"📊 Today's Nutrition Summary:\n\n"
                f"• Total Entries: {calories_count}\n"
                f"• Total Calories: {total_calories:.0f}\n"
                f"• Average per entry: {total_calories/calories_count:.0f} calories"
            )
        else:
            await update.message.reply_text("📭 No food entries found for today!")
            
    except Exception as e:
        print(f"Error in show_todays_calories: {e}")
        await update.message.reply_text("❌ Error fetching today's calories. Please try again.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled!", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# 🏁 Main Function
def main():
    print("🤖 Starting Nutrition Telegram Bot...")
    
    # Start health check server in a separate thread
    health_thread = threading.Thread(target=run_health_check, daemon=True)
    health_thread.start()
    
    # Start Telegram bot
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^(🍎 Log Food|📊 Calculate BMR|📝 Free Form Input)$'), handle_message)],
        states={
            FOOD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, food_input)],
            SERVING_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, serving_input)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_input)],
            GENDER: [MessageHandler(filters.Regex('^(Male|Female)$'), gender_input)],
            WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, weight_input)],
            HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, height_input)],
            ACTIVITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, activity_input)],
            FREE_FORM_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_form_input)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.Regex('^(📈 Today\'s Calories)$'), show_todays_calories))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Bot is running with health check at port 5000")
    application.run_polling()

if __name__ == "__main__":
    main()