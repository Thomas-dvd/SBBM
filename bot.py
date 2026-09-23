import discord
import os
import json
import sqlite3
from google import genai
from google.genai import types
from dotenv import load_dotenv
from datetime import datetime, timedelta
from pydantic import BaseModel
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo



# =======================================================
# GLOBAL VARIABLES
# =======================================================

users_sessions = {}
platform_map = {}
relaunched = 0

@dataclass
class UserSessionData:
    user_name: str = ""
    chat_history: list[types.Content] | None = None        # La session Gemini
    user_platform_id: int = 0
    isoday_last_message: date | None = None   # La date du dernier message
    day_0_date: str = ""
    day_0_content: str = ""
    day_1_date: str = ""
    day_1_content: str = ""

with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

def db():
    # Cette fonction se connecte ET allume le videur automatiquement
    conn = sqlite3.connect(config["database"])
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn



# =======================================================
# Traitement du message
# =======================================================

async def on_request(message, user_platform_id):

    # Vérifie qu'il n'y a pas eu de redémarrage
    if relaunched == 0:
        await message.channel.send("Le bot a redémarré.") 
        relaunched = 1

    # Gestion des commandes
    if message.content.startswith('!'):

        # !database
        if message.content.startswith('!database'):
            db_path = config["database"]
            if os.path.exists(db_path):
                await message.channel.send(file=discord.File(db_path))
                return "📁 Voici la sauvegarde actuelle de ta base de données :"
            else:
                return "❌ Fichier de base de données introuvable sur le VPS."
        
        # !help
        elif message.content.startswith('!help'):
            return "Liste des commandes:\n\n- !database: télécharger la base de donnée\n- !update <nom> <calories> <protéines> <glucides> <lipides> <sel> <fibres> <proportions>\n- !help: afficher ce message"
        
        # !update
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
                conn = sqlite3.connect(config["database"])
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
            return "❌ Commande inconnu. '!help' pour plus d'informations."
        
    if user_platform_id not in platform_map: # Utilisateur pas dans la ram
        user_id = load_state(user_platform_id)
        if user_id is None:
            return "❌ Utilisateur non enregristré."
            #TODO: Système d'inscription
    else:
        user_id = platform_map[user_platform_id]

    try:

        # Vérification de la date
        current_day = day_calculator(datetime.now().astimezone(ZoneInfo("Europe/Paris")))
        if users_sessions[user_id].isoday_last_message != current_day:
            await daily_diary_entry(user_id, current_day)
            
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
                generation_config={"thinking_level": "high"},
                history=restored_history if restored_history else None
            )

        chat_session = users_sessions[user_id]
        response = await chat_session.send_message(request) # On envois à l'IA
        
        # On renvoie la réponse
        return response.text

    except Exception as e:
        print(f"Erreur API : {e}")
        return "❌ Oups, j'ai eu un problème pour contacter le cerveau Google."



# =======================================================
# Gestion de la mémoire
# =======================================================

# Load
def load_state(user_platform_id):
    try:
        conn = db()
        cursor = conn.cursor()

        cursor.execute('SELECT user_id, user_name FROM users WHERE user_platform_id = ?', (user_platform_id,))
        row_user = cursor.fetchone()

        if not row_user:
            print("Utilisateur innexistant")
            conn.close()
            return None

        user_id = row_user[0]
        user_name = row_user[1]

        cursor.execute('SELECT chat_history, isoday_last_message  FROM active_sessions WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()

        if row: # Déjà une session

            raw_history = json.loads(row[0]) if row[0] else []
            gemini_history = []
            for msg in raw_history:
                gemini_history.append(
                    types.Content(
                        role=msg["role"], # "user" ou "model"
                        parts=[types.Part.from_text(text=msg["text"])]
                    )
                )



            users_sessions[user_id] = UserSessionData(
                chat_history = gemini_history,
                user_name= user_name,
                user_platform_id=user_platform_id,
                isoday_last_message = date.fromisoformat(row[1])
            )
            
            cursor.execute('''SELECT daily_commentary,  date
                FROM food_daily_diary 
                WHERE user_id = ? 
                ORDER BY date DESC 
                LIMIT 2
                ''', (user_id,))
            rows = cursor.fetchall()

            if len(rows) > 0:
                users_sessions[user_id].day_0_content=rows[0][0]
                users_sessions[user_id].day_0_date=date.fromisoformat(rows[0][1])

            if len(rows) > 1:
                users_sessions[user_id].day_1_content=rows[1][0]
                users_sessions[user_id].day_1_date=date.fromisoformat(rows[1][1])

        else: # Nouvelle session
            print(f"Création d'une nouvelle session pour {user_name}")
            users_sessions[user_id] = UserSessionData(
                chat_history= [],
                user_name= user_name,
                user_platform_id=user_platform_id,
                isoday_last_message = day_calculator(datetime.now().astimezone(ZoneInfo("Europe/Paris"))) # Date d'aujourd'hui
            )
         
        conn.close()
        platform_map[user_platform_id] = user_id
        return user_id
        
    except Exception as e:
        print(f"⚠️ Erreur lors du chargement de la base de donnée : {e}")

# Gestion de la save de la journée
def save_chat_history(user_id):

    clean_history = []
    for msg in users_sessions[user_id].chat_history:
        text_parts = [p.text for p in msg.parts if p.text is not None]
        if text_parts:
            clean_history.append({
                "role": msg.role,
                "text": "\n".join(text_parts)
            })

    # 2. On le transforme en chaîne de caractères JSON
    history_json_str = json.dumps(clean_history)

    # 3. On sauvegarde dans la base de données
    try:
        conn = db()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE active_sessions 
            SET chat_history = ?
            WHERE user_id = ?
        ''', (
            user_id, 
            history_json_str, 
        ))
        
        conn.commit()
        conn.close()
        
        print(f"💾 Historique de l'utilisateur {user_id} sauvegardé avec succès !")
        
    except Exception as e:
        print(f"❌ Erreur lors de la sauvegarde SQL : {e}")




# =======================================================
# Bilan journaliers
# =======================================================

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

async def daily_diary_entry(user_id, current_day):
    print(f"🔄 Changement de jour détecté pour {users_sessions[user_id].user_name} ({users_sessions[user_id].isoday_last_message} -> {current_day})")
                
    config_diary_prompt = types.GenerateContentConfig(
        system_instruction=config["summary_prompt"],
        response_mime_type="application/json",
        response_schema=BilanJournalier # Schéma précis de réponse
    )

    diary_chat_session = gemini_client.aio.chats.create(
        model=config["model_resume"],
        config=config_diary_prompt,
        history=users_sessions[user_id].chat_history
    )
        
    response = await diary_chat_session.send_message("Génère maintenant le bilan JSON de cette journée.")
    # TODO: Faire une division en 3 requête, une pour les macro/micro, une pour le résumé des aliments consommées, une pour la modification des notes globales
    data = json.loads(response.text)
    data["date"] = str(users_sessions[user_id].isoday_last_message)

    try:
        conn = sqlite3.connect(config["database"])
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

    diary["avant_hier"] = diary["hier"]
    diary["hier"] = data["daily_sum_up"]

    # 6. On nettoie la session pour repartir à zéro
    del users_sessions[user_id]
    users_last_message_day[user_id] = current_day
    save_state()

    conn = db()
    conn.execute('''DELETE FROM "active_sessions" WHERE "user_id" = ?''', (user_id,))
    conn.commit()
    conn.close()

# =======================================================
# Outils additionels
# =======================================================

def day_calculator(message_date): # Si le message a été envoyé entre 00h00 et 05h59, on considère que c'est la journée d'hier
    if message_date.hour < 6:
        date = message_date - timedelta(days=1)
    else:
        date = message_date
        
    # On renvoie juste la date (Année-Mois-Jour) sans les heures/minutes
    return date.date()

def get_nutrition_info(food: str) -> str: # Function Calling: Calorie reading
    
    try:
        conn = db()
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


# LAUNCH BOT
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

if config["app"] == "discord":
    # 1. Chargement des variables d'environnement
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

    # 2. Configuration de Discord
    intents = discord.Intents.default()
    intents.message_content = True
    discord_client = discord.Client(intents=intents)

    @discord_client.event
    async def on_ready():
        load_state()
        print(f'✅ Bot connecté avec succès en tant que {discord_client.user} !')
        print('Attente de messages en Message Privé (DM)...')


    # 3. Gestion des messages discord
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
                response = await on_request(message, message.author.id)
                await message.channel.send(response)

    # 4. Lancement du bot
    discord_client.run(DISCORD_TOKEN)
