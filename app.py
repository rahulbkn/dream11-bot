import random
import time
import os
import json
import sqlite3
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, ConversationHandler
from telegram import ReplyKeyboardMarkup

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRICAPI_KEY = "YOUR_CRICKET_API_KEY"
Q_TABLE_FILE = "q_table.json"
DB_FILE = "player_stats.db"

# Conversation states
PITCH, PLAYERS, MATCH, RATE = range(4)

# Player pool and Q-table
players = []
q_table = {}

def load_q_table():
    global q_table
    if os.path.exists(Q_TABLE_FILE):
        with open(Q_TABLE_FILE, "r") as f:
            q_table = json.load(f)
    else:
        q_table = {}

def save_q_table():
    with open(Q_TABLE_FILE, "w") as f:
        json.dump(q_table, f)

def setup_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS players (
                 name TEXT PRIMARY KEY, role TEXT, credits REAL, team TEXT, 
                 runs INTEGER, avg REAL, sr REAL, last_updated REAL)''')
    conn.commit()
    conn.close()

def cache_player_stats(player):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO players VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (player["name"], player["role"], player["credits"], player["team"],
               player["runs"], player["avg"], player["sr"], time.time()))
    conn.commit()
    conn.close()

def get_cached_player(name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE name = ? AND last_updated > ?", (name, time.time() - 86400))  # 24-hour cache
    result = c.fetchone()
    conn.close()
    return dict(zip(["name", "role", "credits", "team", "runs", "avg", "sr", "last_updated"], result)) if result else None

def setup_selenium():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)

def fetch_player_stats_cricapi(player_name):
    try:
        url = f"https://cricapi.com/api/playerFinder?apikey={CRICAPI_KEY}&name={player_name.replace(' ', '+')}"
        response = requests.get(url).json()
        pid = response["data"][0]["pid"]
        
        stats_url = f"https://cricapi.com/api/playerStats?apikey={CRICAPI_KEY}&pid={pid}"
        stats = requests.get(stats_url).json()
        odi_stats = stats["data"]["batting"]["ODIs"]
        
        role = stats["playingRole"].split(" ")[0]  # e.g., "Batsman", "Bowler"
        credits = min(10.0, max(7.0, float(odi_stats.get("Ave", 20)) / 5))
        return {
            "name": player_name,
            "role": role,
            "credits": credits,
            "runs": int(odi_stats.get("Runs", 0).replace(",", "")),
            "avg": float(odi_stats.get("Ave", 0)),
            "sr": float(odi_stats.get("SR", 0))
        }
    except Exception as e:
        print(f"CricAPI error for {player_name}: {e}")
        return None

def fetch_player_stats_selenium(player_name, driver):
    try:
        driver.get(f"https://search.espncricinfo.com/ci/content/player/search.html?search={player_name.replace(' ', '+')}")
        time.sleep(2)
        player_link = driver.find_element(By.CLASS_NAME, "playerName")
        player_id = player_link.get_attribute("href").split("/")[-1].split(".")[0]

        driver.get(f"https://www.espncricinfo.com/player/{player_name.replace(' ', '-')}-{player_id}")
        time.sleep(2)
        stats_table = driver.find_element(By.XPATH, "//div[contains(@class, 'ds-p-0')]//table")
        rows = stats_table.find_elements(By.TAG_NAME, "tr")[1]
        cols = rows.find_elements(By.TAG_NAME, "td")
        
        runs = int(cols[6].text.replace(",", ""))
        avg = float(cols[8].text)
        sr = float(cols[9].text)
        role = "Batsman"  # Simplified
        credits = min(10.0, max(7.0, avg / 5))
        return {"name": player_name, "role": role, "credits": credits, "runs": runs, "avg": avg, "sr": sr}
    except Exception as e:
        print(f"Selenium error for {player_name}: {e}")
        return None

def initialize_player_pool(player_names, driver):
    global players
    players = []
    for name in player_names:
        cached = get_cached_player(name)
        if cached:
            stats = cached
        else:
            stats = fetch_player_stats_cricapi(name) or fetch_player_stats_selenium(name, driver)
            if stats:
                cache_player_stats(stats)
        if stats:
            stats["team"] = random.choice(["TeamA", "TeamB"])
            players.append(stats)
            if name not in q_table:
                q_table[name] = {"selection_score": 0.5}
    save_q_table()

def update_q_table(team, performance_score):
    learning_rate = 0.1
    for player in team:
        current_score = q_table[player["name"]]["selection_score"]
        reward = performance_score
        new_score = current_score + learning_rate * (reward - current_score)
        q_table[player["name"]]["selection_score"] = max(0.1, min(1.0, new_score))
    save_q_table()

def genetic_algorithm_team(pitch_weights, population_size=50, generations=20):
    def fitness(team):
        if len(team) != 11 or sum(p["credits"] for p in team) > 100:
            return 0
        team_a = sum(1 for p in team if p["team"] == "TeamA")
        team_b = sum(1 for p in team if p["team"] == "TeamB")
        if team_a > 7 or team_b > 7:
            return 0
        wk = sum(1 for p in team if p["role"] == "Wicketkeeper")
        bat = sum(1 for p in team if p["role"] == "Batsman")
        bowl = sum(1 for p in team if p["role"] == "Bowler")
        ar = sum(1 for p in team if p["role"] == "All-rounder")
        if not (1 <= wk <= 4 and 3 <= bat <= 6 and 3 <= bowl <= 6 and 1 <= ar <= 4):
            return 0
        return sum(pitch_weights[p["role"]] * q_table[p["name"]]["selection_score"] * p["avg"] for p in team)

    population = [random.sample(players, 11) for _ in range(population_size)]
    for _ in range(generations):
        population = sorted(population, key=fitness, reverse=True)
        next_gen = population[:10]  # Elitism
        while len(next_gen) < population_size:
            parent1, parent2 = random.sample(population[:20], 2)
            crossover = random.randint(1, 10)
            child = parent1[:crossover] + [p for p in parent2 if p not in parent1[:crossover]][:11 - crossover]
            if random.random() < 0.1:  # Mutation
                child[random.randint(0, 10)] = random.choice([p for p in players if p not in child])
            next_gen.append(child)
        population = next_gen
    return sorted(population, key=fitness, reverse=True)[0]

def generate_team_combinations(pitch_type, num_combinations=20):
    if "spin" in pitch_type.lower():
        weights = {"Bowler": 0.6, "All-rounder": 0.2, "Batsman": 0.15, "Wicketkeeper": 0.05}
    elif "batting" in pitch_type.lower():
        weights = {"Batsman": 0.5, "All-rounder": 0.3, "Bowler": 0.15, "Wicketkeeper": 0.05}
    elif "bowling" in pitch_type.lower():
        weights = {"Bowler": 0.5, "All-rounder": 0.3, "Batsman": 0.15, "Wicketkeeper": 0.05}
    else:
        weights = {"Batsman": 0.25, "All-rounder": 0.25, "Bowler": 0.25, "Wicketkeeper": 0.25}

    combinations = []
    for _ in range(num_combinations):
        team = genetic_algorithm_team(weights)
        combinations.append(team)
        update_q_table(team, random.uniform(0, 1))  # Simulated; replace with real feedback
    return combinations[:num_combinations]

def start(update, context):
    update.message.reply_text(
        "Welcome to the Ultimate Dream11 Bot! What pitch type? (e.g., 'batting-friendly', 'spin-heavy')",
        reply_markup=ReplyKeyboardMarkup([["batting-friendly", "bowling-friendly", "balanced", "spin-heavy"]], one_time_keyboard=True)
    )
    return PITCH

def pitch(update, context):
    context.user_data["pitch"] = update.message.text
    update.message.reply_text("Enter player names (comma-separated) or 'default' for a sample set:")
    return PLAYERS

def players(update, context):
    player_input = update.message.text.strip()
    if player_input.lower() == "default":
        context.user_data["players"] = ["Virat Kohli", "Joe Root", "Jasprit Bumrah", "Ben Stokes"]
    else:
        context.user_data["players"] = [p.strip() for p in player_input.split(",")]
    
    update.message.reply_text("Enter match ID (from CricAPI) or 'skip' for generic teams:")
    return MATCH

def match(update, context):
    match_input = update.message.text.strip()
    context.user_data["match"] = match_input if match_input.lower() != "skip" else None

    pitch_type = context.user_data["pitch"]
    player_names = context.user_data["players"]
    
    update.message.reply_text(f"Generating 20 teams for {pitch_type} pitch...")
    load_q_table()
    setup_db()
    driver = setup_selenium()
    initialize_player_pool(player_names, driver)
    driver.quit()

    teams = generate_team_combinations(pitch_type)
    context.user_data["teams"] = teams
    
    for i, team in enumerate(teams, 1):
        team_str = f"Team {i}:\n" + "\n".join([f"{p['name']} ({p['role']}, {p['credits']} credits, {p['team']})" for p in team])
        update.message.reply_text(team_str)
    
    update.message.reply_text("Rate a team with /rate <team_number> <score> (0-1)")
    return ConversationHandler.END

def rate(update, context):
    try:
        _, team_num, score = update.message.text.split()
        team_num = int(team_num) - 1
        score = float(score)
        if 0 <= team_num < len(context.user_data["teams"]) and 0 <= score <= 1:
            update_q_table(context.user_data["teams"][team_num], score)
            update.message.reply_text(f"Team {team_num + 1} rated with score {score}")
        else:
            update.message.reply_text("Invalid team number or score.")
    except Exception as e:
        update.message.reply_text(f"Error: {e}. Use format: /rate <team_number> <score>")
    return ConversationHandler.END

def cancel(update, context):
    update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PITCH: [MessageHandler(Filters.text & ~Filters.command, pitch)],
            PLAYERS: [MessageHandler(Filters.text & ~Filters.command, players)],
            MATCH: [MessageHandler(Filters.text & ~Filters.command, match)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("rate", rate))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()