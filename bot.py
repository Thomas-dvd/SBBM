import discord
import os
import json
import sqlite3
from google import genai
from google.genai import types
from dotenv import load_dotenv
from datetime import datetime, timedelta

# VARIABLES
users_sessions = {}
users_last_message_day = {}
history = {
    "hier": "Phase de débug. Si tu lit ce message, dit que ce jour la j'ai manger un brocolis.",
    "avant_hier": "Phase de débug. Si tu lit ce message, dit que ce jour la j'ai manger une carrote."
}

with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

# ADDITIONAL TOOLS
def day_calculator(message_date):
    # Si le message a été envoyé entre 00h00 et 05h59, on considère que c'est la journée d'hier
    if message_date.hour < 6:
        date = message_date - timedelta(days=1)
    else:
        date = message_date
        
    # On renvoie juste la date (Année-Mois-Jour) sans les heures/minutes
    return date.date()

# Function Calling: DB
def get_nutrition_info(food: str) -> str:
    
    try:
        conn = sqlite3.connect(config["Tools"]["DB_tool"]["database"])
        cursor = conn.cursor()
        
        cursor.execute(
            '''SELECT * FROM "Calories informations" WHERE "Name" = ? COLLATE NOCASE''',
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
        return f"Erreur de lecture de la base de données : {e}"

get_nutrition_info.__doc__ = config["Tools"]["DB_tool"]["doc"]


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
            try:
                user_id = message.author.id

                # Vérification de la date
                current_day = day_calculator(message.created_at.astimezone())
                if user_id in users_last_message_day:

                    # Si c'est un nouveau jour
                    if users_last_message_day[user_id] != current_day:
                        print(f"🔄 Changement de jour détecté pour {message.author.name} ({users_last_message_day[user_id]} -> {current_day})")
                        chat_session = users_sessions[user_id]
                            
                        # 1. On demande le résumé de la veille en secret à Gemini
                        response_resume = await chat_session.send_message(config["summary_prompt"])
                        
                        # 2. On met à jour notre fausse BDD
                        history["avant_hier"] = history["hier"]
                        history["hier"] = response_resume.text
                        print(f"💾 Résumé d'hier sauvegardé automatiquement : {response_resume.text}")
                        
                        # 3. On détruit l'ancienne session pour forcer la recréation avec le nouveau prompt
                        del users_sessions[user_id]

                        users_last_message_day[user_id] = current_day
                else:
                    # Premier message de la session
                    print(f"👋 Premier message de {message.author.name}, initialisation au jour : {current_day}")
                    users_last_message_day[user_id] = current_day


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

                # On envoie notre liste (qui contient soit [Texte], soit [Texte, Image])
                response = await on_request(user_id, request)
                
                # On renvoie la réponse sur Discord
                await message.channel.send(response.text)
                
            except Exception as e:
                print(f"Erreur API : {e}")
                await message.channel.send("❌ Oups, j'ai eu un problème pour contacter le cerveau Google.")

# Requête Gemini
async def on_request(user_id, request):

    temp = config["main_prompt"].replace("{historique}", f"- Hier : {history['hier']}\n- Avant-hier : {history['avant_hier']}")

    config_prompt = types.GenerateContentConfig(
        system_instruction=temp,
        tools=[get_nutrition_info]
    )

    if user_id not in users_sessions:
        print(f"Création d'une nouvelle mémoire pour l'utilisateur {user_id}")
        users_sessions[user_id] = gemini_client.aio.chats.create(model=config["model_chat"], config=config_prompt)
    
    chat_session = users_sessions[user_id]
    response = await chat_session.send_message(request)
    return response

# END. Lancement du bot
discord_client.run(DISCORD_TOKEN)