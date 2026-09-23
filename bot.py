import discord
import os
import json
import sqlite3
from google import genai
from google.genai import types
from dotenv import load_dotenv
from datetime import datetime, timedelta
from pydantic import BaseModel



# VARIABLES
users_sessions = {}
diary = {
    "hier": "Inconnu",
    "avant_hier": "Inconnu"
}
relaunched = 0

with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

# state.json
STATE_FILE = config["bot_state"]

def save_state():
    # On convertit les clés (int) et les dates (datetime.date) en strings pour le JSON
    last_day_str = {str(k): str(v) for k, v in users_last_message_day.items()}
    
    data = {
        "users_last_message_day": last_day_str,
        "history": history
    }
    
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def load_state():
    global users_last_message_day, history
    
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            
            # Restauration de l'historique
            history = data.get("history", {"hier": "", "avant_hier": ""})
            
            # Restauration et reconversion des dates
            last_day_raw = data.get("users_last_message_day", {})
            users_last_message_day = {}
            for k, v in last_day_raw.items():
                # On remet l'ID en int et la date en objet datetime.date
                users_last_message_day[int(k)] = datetime.strptime(v, "%Y-%m-%d").date()
        print("💾 État du bot restauré depuis state.json !")
    else:
        # Initialisation par défaut (si le fichier JSON n'existe pas encore)
        users_last_message_day = {}
        history = {
            "hier": config["initial_prompt"],
            "avant_hier": "Vide"
        }

def save_session_to_db(user_id, chat_session):
    try:
        history = chat_session.get_history()
        
        # On extrait uniquement le texte (on ignore les bytes des images)
        clean_history = []
        for msg in history:
            text_parts = [p.text for p in msg.parts if p.text is not None]
            if text_parts:
                clean_history.append({
                    "role": msg.role,
                    "text": "\n".join(text_parts)
                })
        
        conn = sqlite3.connect(config["Tools"]["DB_tool"]["database"])
        cursor = conn.cursor()
        # INSERT OR REPLACE écrase l'ancienne sauvegarde grâce à la PRIMARY KEY
        cursor.execute(
            '''INSERT OR REPLACE INTO "active_sessions" ("user_id", "history_json") VALUES (?, ?)''',
            (user_id, json.dumps(clean_history))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Impossible de sauvegarder la session en BDD : {e}")

def load_session_from_db(user_id):
    try:
        conn = sqlite3.connect(config["Tools"]["DB_tool"]["database"])
        cursor = conn.cursor()
        cursor.execute('''SELECT "history_json" FROM "active_sessions" WHERE "user_id" = ?''', (user_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            raw_history = json.loads(row[0])
            gemini_history = []
            
            # On recrée les vrais objets de la librairie Google
            for msg in raw_history:
                gemini_history.append(
                    types.Content(
                        role=msg["role"],
                        parts=[types.Part.from_text(text=msg["text"])]
                    )
                )
            print(f"🔄 Historique rechargé depuis la BDD pour l'utilisateur {user_id}.")
            return gemini_history
            
    except Exception as e:
        print(f"⚠️ Erreur de chargement de la session : {e}")
    
    return [] # Si rien n'est trouvé ou en cas d'erreur, on renvoie une liste vide



# ADDITIONAL TOOLS
def day_calculator(message_date): # Si le message a été envoyé entre 00h00 et 05h59, on considère que c'est la journée d'hier
    if message_date.hour < 6:
        date = message_date - timedelta(days=1)
    else:
        date = message_date
        
    # On renvoie juste la date (Année-Mois-Jour) sans les heures/minutes
    return date.date()

def get_nutrition_info(food: str) -> str: # Function Calling: Calorie reading
    
    try:
        conn = sqlite3.connect(config["Tools"]["DB_tool"]["database"])
        cursor = conn.cursor()
        
        cursor.execute(
            '''SELECT * FROM calories_informations WHERE "Name" = ? COLLATE NOCASE''',
            (food,)
        )
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            name, cal, prot, carbo, fat, salt, fiber, proportion = row
            return (f"INFO BDD: Pour 100g de {name} -> {cal} kcal, "
                    f"Prot: {prot}g, Gluc: {carbo}g, Lip: {fat}g, Sel: {salt}g, Fibre: {fiber}g, "
                    f"Portion standard : {proportion}g.")
        else:
            return f"INFO BDD: L'aliment '{food}' est introuvable. Tu dois estimer les valeurs toi-même."
            
    except Exception as e:
        return f"INFO BDD: Erreur imprévue de lecture de la base de données : {e}, merci d'en informer l'utilisateur."

get_nutrition_info.__doc__ = config["Tools"]["DB_tool"]["doc"]

# Diary writing
class BilanJournalier(BaseModel): 
    calories: float
    proteins: float
    carbohydrates: float
    fat: float
    fiber: float
    zinc: float
    iron: float
    magnesium: float
    potassium: float
    calcium: float
    sodium: float
    b12: float
    b6: float  
    b9: float
    c: float
    d: float
    a: float
    e: float
    k: float
    omega3: float
    omega6: float
    daily_sum_up: str

def daily_diary_registration(daily_data):
    try:
        conn = sqlite3.connect(config["Tools"]["DB_tool"]["database"])
        cursor = conn.cursor()
        
        # On prépare la requête dynamiquement
        colonnes = ', '.join([f'"{key}"' for key in daily_data.keys()])
        # Note: On ajuste un peu les noms pour coller à ta casse SQL
        
        placeholders = ', '.join(['?' for _ in daily_data])
        values = list(daily_data.values())
        
        sql = f'INSERT INTO nutritional_assessment ({colonnes}) VALUES ({placeholders})'
        
        cursor.execute(sql, values)
        conn.commit()
        conn.close()
        print(f"✅ Bilan du {daily_data['date']} sauvegardé en BDD !")
    except Exception as e:
        print(f"❌ Erreur sauvegarde SQL : {e}")



# LAUNCH BOT DISCORD
# 1. Chargement des variables d'environnement
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 2. Configuration du NOUVEAU client Google Gemini
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 3. Configuration de Discord
intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

@discord_client.event
async def on_ready():
    load_state()
    print(f'✅ Bot connecté avec succès en tant que {discord_client.user} !')
    print('Attente de messages en Message Privé (DM)...')


# 4. Gestion des messages discord
@discord_client.event
async def on_message(message):
    # On ignore les messages envoyés par le bot lui-même
    if message.author == discord_client.user:
        return

    # On écoute uniquement les Messages Privés (DM)
    if isinstance(message.channel, discord.DMChannel):
        print(f"Message reçu de {message.author.name}: {message.content}")
        
        # Affiche "Le bot est en train d'écrire..." sur Discord
        async with message.channel.typing():
            response = await on_request(message)
            await message.channel.send(response)



# GEMINI
async def on_request(message):

    # Vérifie qu'il n'y a pas eu de redémarrage
    global relaunched
    if relaunched == 0:
        await message.channel.send("Le bot a redémarré.") 
        relaunched = 1

    # Commandes
    if message.content.startswith('!'):

        # !database
        if message.content.startswith('!database'):
            db_path = config["Tools"]["DB_tool"]["database"]
            if os.path.exists(db_path):
                await message.channel.send(file=discord.File(db_path))
                return "📁 Voici la sauvegarde actuelle de ta base de données :"
            else:
                return "❌ Fichier de base de données introuvable sur le VPS."
        
        # !help
        elif message.content.startswith('!help'):
            return "Liste des commandes:\n\n- !database: télécharger la base de donnée\n- !update <nom> <calories> <protéines> <glucides> <lipides> <sel> <fibres> <proportions>\n- !help: afficher ce message"
        
        #!update
        elif message.content.startswith('!update'):
            raw_data = message.content[len('!update '):].strip()
        
            # On découpe tout par les espaces
            parts = raw_data.split()
            
            # Il faut au minimum 1 mot pour le nom + 7 valeurs
            if len(parts) < 8:
                return "❌ **Format incorrect.**\nAttendu : `!update <nom> <calories> <protéines> <glucides> <lipides> <sel> <fibres> <proportions>`."
            
            try:
                # On isole les 7 dernières valeurs et on remplace les virgules par des points
                macros_str = parts[-7:]
                macros = [float(val.replace(',', '.')) for val in macros_str]
                cal, prot, carbo, fat, salt, fiber, proportion = macros
                
                # Le nom correspond à tout le reste (du début jusqu'aux 7 derniers éléments)
                name = " ".join(parts[:-7])
                
                # Connexion à la BDD et insertion
                conn = sqlite3.connect(config["Tools"]["DB_tool"]["database"])
                cursor = conn.cursor()
                
                cursor.execute(
                    '''INSERT INTO calories_informations 
                    ("name", "calories", "proteins", "carbohydrates", "fat", "salt", "fiber", "default_proportion") 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (name, cal, prot, carbo, fat, salt, fiber, proportion)
                )
                conn.commit()
                conn.close()
                
                return f"✅ L'aliment **{name}** a été ajouté avec succès !"
            
            except ValueError:
                return "❌ **Erreur :** Impossible de lire les chiffres. Assure-toi que les 7 dernières valeurs sont bien des nombres (ex: 2.5 ou 2,5)."
            except Exception as e:
                return f"❌ **Erreur SQL :** {e}"
                
        else:
            return "Commande inconnu. '!help' pour plus d'informations."
        
    try:
        user_id = message.author.id

        # Vérification de la date
        current_day = day_calculator(message.created_at.astimezone())
        if user_id in users_last_message_day:

            # Si c'est un nouveau jour
            if users_last_message_day[user_id] != current_day:
                print(f"🔄 Changement de jour détecté pour {message.author.name} ({users_last_message_day[user_id]} -> {current_day})")
                
                # Config session alternative avec le modèle plus lourd
                
                chat_session = users_sessions[user_id]

                g_history = chat_session.get_history()

                config_diary_prompt = types.GenerateContentConfig(
                    system_instruction=config["summary_prompt"],
                    response_mime_type="application/json",
                    response_schema=BilanJournalier # Le schéma qu'on a créé au début
                )

                chat_session = gemini_client.aio.chats.create(
                    model=config["model_resume"],
                    config=config_diary_prompt,
                    history=g_history
                )
                    
                response_resume = await chat_session.send_message("Génère maintenant le bilan JSON de cette journée.")
                
                data = json.loads(response_resume.text)
                data["date"] = str(users_last_message_day[user_id])

                daily_diary_registration(data)

                diary["avant_hier"] = diary["hier"]
                diary["hier"] = data["daily_sum_up"]

                # 6. On nettoie la session pour repartir à zéro
                del users_sessions[user_id]
                users_last_message_day[user_id] = current_day
                save_state()

                conn = sqlite3.connect(config["Tools"]["DB_tool"]["database"])
                conn.execute('''DELETE FROM "active_sessions" WHERE "user_id" = ?''', (user_id,))
                conn.commit()
                conn.close()

        else:
            # Premier message de la session
            print(f"👋 Premier message de {message.author.name}, initialisation au jour : {current_day}")
            users_last_message_day[user_id] = current_day
            save_state()


        # On prépare la liste de ce qu'on va envoyer à Gemini
        request = [message.content]

        # On vérifie s'il y a une pièce jointe dans le message
        if message.attachments:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    print(f"📷 Image détectée : {attachment.filename}")
                    
                    # On télécharge les données
                    image_bytes = await attachment.read()
                    
                    # On l'ajoute à la liste pour Gemini
                    request.append(
                        types.Part.from_bytes(
                            data=image_bytes,
                            mime_type=attachment.content_type
                        )
                    )
                else:
                    print(f"⚠️ Fichier ignoré (pas une image) : {attachment.filename}")

        temp = config["main_prompt"].replace("{diary}", f"- Hier : {diary['hier']}\n- Avant-hier : {diary['avant_hier']}")

        config_prompt = types.GenerateContentConfig(
            system_instruction=temp,
            tools=[get_nutrition_info]
        )

        # Premier message de la journée
        if user_id not in users_sessions:
            restored_history = load_session_from_db(user_id)

            print(f"Nouvelle journée pour l'utilisateur {user_id}")
            users_sessions[user_id] = gemini_client.aio.chats.create(
                model=config["model_chat"],
                config=config_prompt,
                history=restored_history if restored_history else None
            )

        chat_session = users_sessions[user_id]
        response = await chat_session.send_message(request)
        
        # On renvoie la réponse
        return response.text

    except Exception as e:
        print(f"Erreur API : {e}")
        return "❌ Oups, j'ai eu un problème pour contacter le cerveau Google."



# END. Lancement du bot
discord_client.run(DISCORD_TOKEN)