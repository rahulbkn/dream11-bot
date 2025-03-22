import random
import time
import os
import json
import sqlite3
import requests
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, ConversationHandler
from telegram import ReplyKeyboardMarkup

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRICAPI_KEY = os.getenv("CRICAPI_KEY")
Q_TABLE_FILE = "q_table.json"
DB_FILE = "player_stats.db"

# Conversation states
PITCH, MATCH, PLAYERS, RATE = range(4)

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
                 runs INTEGER, avg REAL, sr REAL, points REAL, last_updated REAL)''')
    conn.commit()
    conn.close()

def cache_player_stats(player):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (player["name"], player["role"], player["credits"], player["team"],
               player["runs"], player["avg"], player["sr"], player.get("points", 0), time.time()))
    conn.commit()
    conn.close()

def get_cached_player(name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE name = ? AND last_updated > ?", (name, time.time() - 86400))
    result = c.fetchone()
    conn.close()
    return dict(zip(["name", "role", "credits", "team", "runs", "avg", "sr", "points", "last_updated"], result)) if result else None

def fetch_current_matches():
    """Fetch list of current/upcoming matches."""
    try:
        url = f"https://api.cricapi.com/v1/currentMatches?apikey={CRICAPI_KEY}&offset=0"
        response = requests.get(url).json()
        return response["data"]
    except Exception as e:
        print(f"Error fetching current matches: {e}")
        return []

def fetch_match_squad(match_id):
    """Fetch squad data for a specific match."""
    try:
        url = f"https://api.cricapi.com/v1/match_squad?apikey={CRICAPI_KEY}&id={match_id}"
        response = requests.get(url).json()
        if "data" not in response or not response["data"]:
            return None
        squad = response["data"]["squad"]
        return squad[0]["players"], squad[1]["players"]  # Team 1 and Team 2
    except Exception as e:
        print(f"Error fetching match squad for {match_id}: {e}")
        return None, None

def fetch_series_squad(series_id):
    """Fetch squad data for a series."""
    try:
        url = f"https://api.cricapi.com/v1/series_squad?apikey={CRICAPI_KEY}&id={series_id}"
        response = requests.get(url).json()
        if "data" not in response or not response["data"]:
            return None
        squad = response["data"]["squad"]
        return squad[0]["players"], squad[1]["players"]  # Team 1 and Team 2
    except Exception as e:
        print(f"Error fetching series squad for {series_id}: {e}")
        return None, None

def fetch_match_scorecard(match_id):
    """Fetch scorecard data for detailed stats."""
    try:
        url = f"https://api.cricapi.com/v1/match_scorecard?apikey={CRICAPI_KEY}&id={match_id}"
        response = requests.get(url).json()
        if "data" not in response or not response["data"]:
            return {}
        return response["data"]["scorecard"]  # Adjust based on actual structure
    except Exception as e:
        print(f"Error fetching scorecard for {match_id}: {e}")
        return {}

def fetch_match_points(match_id):
    """Fetch Dream11 points for players."""
    try:
        url = f"https://api.cricapi.com/v1/match_points?apikey={CRICAPI_KEY}&id={match_id}&ruleset=0"
        response = requests.get(url).json()
        if "data" not in response or not response["data"]:
            return {}
        return response["data"]["fantasy_points"]  # Adjust based on actual structure
    except Exception as e:
        print(f"Error fetching match points for {match_id}: {e}")
        return {}

def initialize_player_pool(match_id=None, series_id=None, player_names=None):
    global players
    players = []
    
    if match_id:
        team1_players, team2_players = fetch_match_squad(match_id)
        if not team1_players or not team2_players:
            return
        scorecard = fetch_match_scorecard(match_id)
        points = fetch_match_points(match_id)
    elif series_id:
        team1_players, team2_players = fetch_series_squad(series_id)
        if not team1_players or not team2_players:
            return
        scorecard = {}
        points = {}
    else:
        team1_players = team2_players = []
        scorecard = {}
        points = {}

    all_players = team1_players + team2_players if team1_players else [ {"name": n, "role": "Unknown"} for n in player_names or [] ]

    for player in all_players:
        name = player["name"]
        cached = get_cached_player(name)
        if cached:
            players.append(cached)
            continue
        
        role = player.get("role", "Batsman")
        team = "TeamA" if player in team1_players else "TeamB"
        
        # Get stats from scorecard if available
        stats = scorecard.get(name, {}) if isinstance(scorecard, dict) else {}
        runs = int(stats.get("runs", 0))
        avg = float(stats.get("average", 0)) or runs  # Fallback to runs if no avg
        sr = float(stats.get("strike_rate", 0))
        
        # Get points for credits
        player_points = points.get(name, {}).get("points", 0) if isinstance(points, dict) else 0
        credits = min(10.0, max(7.0, player_points / 10 or avg / 5))

        player_data = {
            "name": name,
            "role": role,
            "credits": credits,
            "team": team,
            "runs": runs,
            "avg": avg,
            "sr": sr,
            "points": player_points
        }
        cache_player_stats(player_data)
        players.append(player_data)
    
    for p in players:
        if p["name"] not in q_table:
            q_table[p["name"]] = {"selection_score": 0.5}
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
        return sum(pitch_weights[p["role"]] * q_table[p["name"]]["selection_score"] * (p["points"] or p["avg"]) for p in team)

    population = [random.sample(players, 11) for _ in range(population_size)]
    for _ in range(generations):
        population = sorted(population, key=fitness, reverse=True)
        next_gen = population[:10]
        while len(next_gen) < population_size:
            parent1, parent2 = random.sample(population[:20], 2)
            crossover = random.randint(1, 10)
            child = parent1[:crossover] + [p for p in parent2 if p not in parent1[:crossover]][:11 - crossover]
            if random.random() < 0.1:
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
        update_q_table(team, random.uniform(0, 1))  # Replace with real points later
    return combinations[:num_combinations]

def start(update, context):
    update.message.reply_text(
        "Welcome to the Ultimate Dream11 Bot! What pitch type? (e.g., 'batting-friendly', 'spin-heavy')",
        reply_markup=ReplyKeyboardMarkup([["batting-friendly", "bowling-friendly", "balanced", "spin-heavy"]], one_time_keyboard=True)
    )
    return PITCH

def pitch(update, context):
    context.user_data["pitch"] = update.message.text
    matches = fetch_current_matches()
    if matches:
        match_list = "\n".join([f"{i+1}. {m['name']} (ID: {m['id']})" for i, m in enumerate(matches[:5])])
        update.message.reply_text(f"Select a match by number or enter a match/series ID:\n{match_list}\nOr type 'skip' for manual players:")
    else:
        update.message.reply_text("Enter a match/series ID or 'skip' for manual players:")
    return MATCH

def match(update, context):
    match_input = update.message.text.strip().lower()
    matches = fetch_current_matches()
    
    if match_input.isdigit() and 1 <= int(match_input) <= len(matches):
        context.user_data["match_id"] = matches[int(match_input) - 1]["id"]
        context.user_data["series_id"] = None
    elif match_input != "skip":
        context.user_data["match_id"] = match_input if "match" in context.user_data.get("last_input", "") else None
        context.user_data["series_id"] = match_input if "series" in context.user_data.get("last_input", "") else None
    else:
        context.user_data["match_id"] = context.user_data["series_id"] = None
    
    context.user_data["last_input"] = match_input
    update.message.reply_text("Enter player names (comma-separated) or 'default' (ignored if match/series ID provided):")
    return PLAYERS

def players(update, context):
    player_input = update.message.text.strip()
    if player_input.lower() == "default" and not (context.user_data["match_id"] or context.user_data["series_id"]):
        context.user_data["players"] = ["Virat Kohli", "Rohit Sharma", "Jasprit Bumrah", "KL Rahul"]
    else:
        context.user_data["players"] = [p.strip() for p in player_input.split(",")] if not (context.user_data["match_id"] or context.user_data["series_id"]) else []

    pitch_type = context.user_data["pitch"]
    match_id = context.user_data["match_id"]
    series_id = context.user_data["series_id"]
    player_names = context.user_data["players"]
    
    update.message.reply_text(f"Generating 20 teams for {pitch_type} pitch...")
    load_q_table()
    setup_db()
    initialize_player_pool(match_id, series_id, player_names)

    if not players:
        update.message.reply_text("Failed to fetch player data. Try again with a valid ID or players.")
        return ConversationHandler.END

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
            MATCH: [MessageHandler(Filters.text & ~Filters.command, match)],
            PLAYERS: [MessageHandler(Filters.text & ~Filters.command, players)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("rate", rate))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
