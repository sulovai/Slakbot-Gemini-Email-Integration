from dotenv import load_dotenv
import os
import pickle
from flask import Flask, request, Response
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from datetime import datetime
from slack_sdk import WebClient
import json
import requests

load_dotenv()

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar.events'
]

app = Flask(__name__)

SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN')
client = WebClient(token=SLACK_BOT_TOKEN)

# ------------------ Google Authentication ------------------

def authenticate():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
        creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return creds

def check_unread_emails(service):
    results = service.users().messages().list(userId='me', labelIds=['INBOX', 'UNREAD'], maxResults=5).execute()
    messages = results.get('messages', [])

    unread_count = len(messages)
    email_summaries = []

    for msg in messages:
        msg_detail = service.users().messages().get(userId='me', id=msg['id']).execute()

        # Get subject from headers
        subject = "No Subject"
        headers = msg_detail.get('payload', {}).get('headers', [])
        for header in headers:
            if header['name'] == 'Subject':
                subject = header['value']
                break

        # Get snippet
        snippet = msg_detail.get('snippet', '')[:150]

        email_summaries.append(f"*Subject:* {subject}\n*Snippet:* {snippet}")

    return unread_count, email_summaries


def create_meeting_event(calendar_service, title, start_time, end_time):
    event = {
        'summary': title,
        'start': {
            'dateTime': start_time.isoformat(),
            'timeZone': 'Asia/Kolkata',
        },
        'end': {
            'dateTime': end_time.isoformat(),
            'timeZone': 'Asia/Kolkata',
        },
        'conferenceData': {
            'createRequest': {
                'requestId': f'unique-id-{start_time.strftime("%Y%m%d%H%M")}',
                'conferenceSolutionKey': {'type': 'hangoutsMeet'}
            }
        }
    }

    event = calendar_service.events().insert(
        calendarId='primary',
        body=event,
        conferenceDataVersion=1
    ).execute()

    return event['conferenceData']['entryPoints'][0]['uri']


#------------------- Trello API ------------------

TRELLO_API_KEY = os.getenv("TRELLO_API_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN")

def get_list_id_by_name(board_id, list_name):
    url = f"https://api.trello.com/1/boards/{board_id}/lists"
    params = {
        "key": TRELLO_API_KEY,
        "token": TRELLO_TOKEN
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    for lst in response.json():
        if lst["name"].lower() == list_name.lower():
            return lst["id"]
    raise Exception(f"List '{list_name}' not found.")

def get_board_id_by_name(board_name):
    url = "https://api.trello.com/1/members/me/boards"
    params = {
        "key": TRELLO_API_KEY,
        "token": TRELLO_TOKEN,
        "fields": "name,id",
    }
    response = requests.get(url, params=params)
    response.raise_for_status()

    for board in response.json():
        if board['name'].lower() == board_name.lower():
            return board['id']
    raise Exception(f"Board '{board_name}' not found.")


def create_trello_card(list_id, name, desc=""):
    url = "https://api.trello.com/1/cards"
    query = {
        'key': TRELLO_API_KEY,
        'token': TRELLO_TOKEN,
        'idList': list_id,
        'name': name,
        'desc': desc
    }
    response = requests.post(url, params=query)
    response.raise_for_status()
    return response.json()

def search_trello_cards(card_name):
    url = "https://api.trello.com/1/search"
    params = {
        "key": TRELLO_API_KEY,
        "token": TRELLO_TOKEN,
        "query": card_name,
        "modelTypes": "cards",
        "card_fields": "name,shortUrl,desc",
        "cards_limit": 5  # Limit number of results
    }

    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json().get("cards", [])

def get_trello_card_by_id(card_id):
    url = f"https://api.trello.com/1/cards/{card_id}"
    params = {
        "key": TRELLO_API_KEY,
        "token": TRELLO_TOKEN,
        "fields": "name,desc,due,url,idList"
    }

    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def gemini_response_user_query(text):
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL = "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""
        You are a helpful assistant in Slack. Answer the user's question based on query
        and provide a JSON response. The user query is: "{text}".
        Format your response as JSON:
        {{
          "response": "Your response here"
        }}
    """
    headers = {
        "Content-Type": "application/json",
    }
    data = {
        "contents": [{
            "parts": [{
                "text": prompt
            }]
        }]
    }

    response = requests.post(url, headers=headers, data=json.dumps(data))
    response.raise_for_status()
    response_json = response.json()

    generated_text = response_json["candidates"][0]["content"]["parts"][0]["text"]
    generated_text = generated_text.replace("```json", "").replace("```", "").strip()
    return json.loads(generated_text)





# ------------------ Slack Endpoints ------------------

@app.route('/hello', methods=['POST'])
def hello():
    channel_id = request.form.get('channel_id')
    client.chat_postMessage(channel=channel_id, text="👋 Hello! How can I assist you today?")
    return Response(status=200)

@app.route('/inbox', methods=['POST'])
def inbox():
    channel_id = request.form.get('channel_id')
    user_id = request.form.get('user_id')

    # Immediately acknowledge
    response = Response("Checking inbox...", status=200)
    
    # Respond asynchronously
    def async_check():
        try:
            creds = authenticate()
            gmail_service = build('gmail', 'v1', credentials=creds)
            count, summaries = check_unread_emails(gmail_service)
            message = f"📬 <@{user_id}> You have *{count}* unread emails."
            if summaries:
                message += "\n\nHere are the latest unread emails:\n\n" + "\n\n".join(summaries)

            client.chat_postMessage(channel=channel_id, text=message)
        except Exception as e:
            client.chat_postMessage(channel=channel_id, text=f"❌ Failed to check inbox: {e}")

    from threading import Thread
    Thread(target=async_check).start()

    return response


def gemini_response(prompt):
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL = "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {
        "Content-Type": "application/json",
    }
    data = {
        "contents": [{
            "parts": [{
                "text": prompt
            }]
        }]
    }

    response = requests.post(url, headers=headers, data=json.dumps(data))
    response.raise_for_status()
    response_json = response.json()

    generated_text = response_json["candidates"][0]["content"]["parts"][0]["text"]
    generated_text = generated_text.replace("```json", "").replace("```", "").strip()
    return json.loads(generated_text)
    

@app.route('/meet', methods=['POST'])
def meet():
    channel_id = request.form.get('channel_id')
    user_id = request.form.get('user_id')
    text = request.form.get('text', '')

    response = Response(f"Creating meeting...Request: {text}", status=200)

    def async_meet():
        try:
            # Step 1: Ask Gemini to extract title, start_time, and end_time
            prompt = f"""Extract the meeting title, start time, and end time from this request:
            
            Request: "{text}"

            Format your response as JSON:
            {{
              "title": "Meeting title here",
              "start_time": "2025-05-10T15:00",
              "end_time": "2025-05-10T15:30"
            }}"""
            
            structured = gemini_response(prompt)

            title = structured['title']
            start_time = datetime.strptime(structured['start_time'], '%Y-%m-%dT%H:%M')
            end_time = datetime.strptime(structured['end_time'], '%Y-%m-%dT%H:%M')

            # Step 2: Authenticate and create meeting
            creds = authenticate()
            calendar_service = build('calendar', 'v3', credentials=creds)
            link = create_meeting_event(calendar_service, title, start_time, end_time)

            client.chat_postMessage(channel=channel_id, text=f"✅ <@{user_id}> Google Meet created: <{link}>")

        except Exception as e:
            client.chat_postMessage(channel=channel_id, text=f"❌ Failed to create meeting: {e}")

    from threading import Thread
    Thread(target=async_meet).start()

    return response

@app.route('/create_card_trello', methods=['POST'])
def trello():
    channel_id = request.form.get('channel_id')
    user_id = request.form.get('user_id')
    text = request.form.get('text').strip()

    response = Response(f"Creating Trello card...Request: {text}", status=200)

    def async_create():
        try:
            parts = text.strip().split(None, 1)
            if len(parts) < 1:
                client.chat_postMessage(channel=channel_id, text="❗ Usage: `/create_card_trello <card_title> [description]`")
                return

            card_title = parts[0]
            card_desc = parts[1] if len(parts) > 1 else ""

            board_id = get_board_id_by_name("My Trello Board")  # Change name as needed
            list_id = get_list_id_by_name(board_id, "To Do")  # Change list name if needed

            card = create_trello_card(list_id, card_title, card_desc)
            client.chat_postMessage(channel=channel_id, text=f"✅ <@{user_id}> Trello card created: <{card['shortUrl']}>")
        except Exception as e:
            client.chat_postMessage(channel=channel_id, text=f"❌ Trello error: {e}")

    from threading import Thread
    Thread(target=async_create).start()
    return response



@app.route('/trellosearch', methods=['POST'])
def trello_search():
    channel_id = request.form.get('channel_id')
    user_id = request.form.get('user_id')
    text = request.form.get('text', '').strip()

    response = Response(f"🔍 Searching Trello cards...Request: {text}", status=200)

    

    def async_search():
        try:
            cards = search_trello_cards(text)
            if not cards:
                client.chat_postMessage(channel=channel_id, text=f"❌ No cards found matching: *{text}*")
                return

            message = f"📋 Found {len(cards)} card(s) for *{text}*:\n"
            for card in cards:
                message += f"• *{card['name']}*\n🔗 <{card['shortUrl']}>\n"

            client.chat_postMessage(channel=channel_id, text=message)

        except Exception as e:
            client.chat_postMessage(channel=channel_id, text=f"❌ Error searching Trello cards: {e}")

    from threading import Thread
    Thread(target=async_search).start()
    return response

@app.route('/trello_card', methods=['POST'])
def trello_card():
    channel_id = request.form.get('channel_id')
    user_id = request.form.get('user_id')
    text = request.form.get('text', '').strip()

    response = Response(f"🔍 Fetching Trello card...Request: {text}", status=200)

    from threading import Thread

    def async_card_lookup():
        try:
            cards = search_trello_cards(text)
            if not cards:
                client.chat_postMessage(channel=channel_id, text=f"❌ No Trello card found with name: *{text}*")
                return

            card = cards[0]  # get the first match
            card_details = get_trello_card_by_id(card['id'])

            message = (
                f"📌 *{card_details['name']}*\n"
                f"📝 {card_details['desc'] or 'No description'}\n"
                f"📅 Due: {card_details.get('due', 'Not set')}\n"
                f"🔗 <{card_details['url']}>"
            )
            client.chat_postMessage(channel=channel_id, text=message)

        except Exception as e:
            client.chat_postMessage(channel=channel_id, text=f"❌ Error retrieving card: {e}")

    Thread(target=async_card_lookup).start()
    return response




@app.route('/gemini', methods=['POST'])
def gemini():
    channel_id = request.form.get('channel_id')
    user_id = request.form.get('user_id')
    text = request.form.get('text', '').strip()

    response = Response(f"🔍 Processing your request in Gemini...Request: {text}", status=200)

    def async_gemini_response():
        try:
            structured_response = gemini_response_user_query(text)
            client.chat_postMessage(channel=channel_id, text=f"💬 <@{user_id}> {structured_response['response']}")
        except Exception as e:
            client.chat_postMessage(channel=channel_id, text=f"❌ Error: {e}")

    from threading import Thread
    Thread(target=async_gemini_response).start()
    return response


from groq import Groq
def create_custom_prompt(user_input):
    return f"""
        You are a helpful assistant in Slack. Answer the user's question based on query
        and provide a JSON response. The user query is: "{user_input}".
        Format your response as JSON:
        {{
          "response": "Your response here"
        }}
    """
def groq_response(prompt):
    client = Groq(api_key="")
    completion = client.chat.completions.create(
        model="llama3-8b-8192",
        messages=[
            {
                "role": "user",
                "content": create_custom_prompt(prompt)
            }
        ],
        temperature=1,
        max_tokens=1024,
        top_p=1,
        stream=True,
        stop=None,
    )

    ret = ""
    for chunk in completion:
        delta = chunk.choices[0].delta
        if hasattr(delta, "content") and delta.content:
             ret += delta.content

        # Clean and parse JSON if the response is JSON-formatted
    ret = ret.replace("```json", "").replace("```", "").strip()
    ret = ret.replace("{", "")
    ret = ret.replace("}", "")
    #print(ret)
    return ret
    
@app.route('/groq', methods=['POST'])
def groq():
    channel_id = request.form.get('channel_id')
    user_id = request.form.get('user_id')
    text = request.form.get('text', '').strip()

    response = Response(f"🔍 Processing your request in Groq... : Request: {text}", status=200)

    def async_groq_response():
        try:
            structured_response = groq_response(text)
            client.chat_postMessage(channel=channel_id, text=f"💬 <@{user_id}> {structured_response}")
        except Exception as e:
            client.chat_postMessage(channel=channel_id, text=f"❌ Error: {e}")

    from threading import Thread
    Thread(target=async_groq_response).start()
    return response




# ------------------ Main ------------------

if __name__ == "__main__":
    app.run(port=3000)
