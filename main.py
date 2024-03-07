from elasticsearch.exceptions import NotFoundError
from elasticsearch import Elasticsearch
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import requests
import openai
import os

es_host = os.getenv('ES_HOST')
es_user = os.getenv('ES_USER')
es_pass = os.getenv('ES_PASS')
openai.api_key = os.getenv('OPEN_AI_KEY')
slack_api = os.getenv('SLACK_API')
channel_id = os.getenv('CHANNEL_ID')
rootly_api = os.getenv('ROOTLY_API')
incident_id = os.getenv('INCIDENT_ID')

es = Elasticsearch(es_host, basic_auth=(es_user, es_pass))

def fetch_incident_from_rootly(incident_id):
    rootly_api_url = f"https://api.rootly.com/v1/incidents/{incident_id}"
    headers = {"Authorization": f"Bearer {rootly_api}"}
    response = requests.get(rootly_api_url, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        print("Failed to fetch incident data:", response.text)
        return None

def index_incident_to_es(es, incident_data):
    index_name = "rootly-incident"
    try:
        if not es.indices.exists(index=index_name):
            es.indices.create(index=index_name)
        incident_id = incident_data["data"]["id"]
        document_to_index = incident_data["data"]["attributes"]
        document_to_index["id"] = incident_id
        res = es.index(index=index_name, id=incident_id, body=document_to_index)
        print("Document indexed successfully:", res)
    except Exception as e:
        print("Error indexing document:", e)

def query_rootly_incidents(es):
    try:
        response = es.search(index="rootly-incident", body={"query": {"match_all": {}}, "sort": [{"started_at": {"order": "asc"}}]})
        return [doc['_source'] for doc in response['hits']['hits']]
    except NotFoundError:
        print("Index 'rootly-incident' does not exist. No incidents to query.")
        return []
    except Exception as e:
        print("Error querying rootly incidents:", e)
        return []

def fetch_events_from_rootly(incident_id):
    rootly_events_url = f"https://api.rootly.com/v1/incidents/{incident_id}/events"
    headers = {"Authorization": f"Bearer {rootly_api}"}
    response = requests.get(rootly_events_url, headers=headers)
    if response.status_code == 200:
        print("Successfully fetched events data.")
        return response.json()["data"]
    else:
        print(f"Failed to fetch events data: {response.text}")
        return []

def index_events_to_es(es, events_data):
    index_name = "rootly-events"
    for event in events_data:
        try:
            if not es.indices.exists(index=index_name):
                es.indices.create(index=index_name)
            event_id = event["id"]
            document_to_index = event.get("attributes", {})
            document_to_index["id"] = event_id
            es.index(index=index_name, id=event_id, body=document_to_index)
        except Exception as e:
            print(f"Error indexing event: {e}")

def query_rootly_events(es):
    try:
        response = es.search(index="rootly-events", body={"query": {"match_all": {}}, "sort": [{"occurred_at": {"order": "asc"}}]})
        return [doc['_source'] for doc in response['hits']['hits']]
    except NotFoundError:
        print("Index 'rootly-events' does not exist. No events to query.")
        return []
    except Exception as e:
        print("Error querying rootly events:", e)
        return []

def fetch_slack_channel_history(channel_id):
    slack_token = slack_api
    client = WebClient(token=slack_token)
    try:
        result = client.conversations_history(channel=channel_id)
        messages = result["messages"]
        print(f"Retrieved {len(messages)} messages.")
        return messages
    except SlackApiError as e:
        print(f"Error fetching messages: {e}")

def preprocess_message_blocks(message):
    if 'blocks' in message:
        for block_idx, block in enumerate(message['blocks']):
            if 'elements' in block:
                for element_idx, element in enumerate(block['elements']):
                    if 'text' in element:
                        # Ensuring 'text' is always treated as an object
                        if isinstance(element['text'], str):
                            # If 'text' is a string, encapsulate it into an object
                            message['blocks'][block_idx]['elements'][element_idx]['text'] = {
                                "text": element['text'],
                                # Add additional fields as needed, for example:
                                "type": "plain_text"  
                            }
                        # If 'text' is already an object with 'text' as a key, it's fine as is
                        # Optionally, handle or transform other keys within 'text' object here
    return message

def index_messages_to_es(es, messages):
    index_name = "slack"
    try:
        if not es.indices.exists(index=index_name):
            es.indices.create(index=index_name)
        for message in messages:
            preprocessed_message = preprocess_message_blocks(message)
            es.index(index=index_name, body=preprocessed_message)
    except Exception as e:
        print("Error indexing message:", e)

def query_slack_messages(es):
    try:
        response = es.search(index="slack", body={"query": {"match_all": {}}, "sort": [{"timestamp": {"order": "asc"}}]})
        return [doc['_source'] for doc in response['hits']['hits']]
    except NotFoundError:
        print("Index 'slack' does not exist. No messages to query.")
        return []
    except Exception as e:
        print("Error querying Slack messages:", e)
        return []

def ask_openai_about_incidents(question, es):
    incidents = query_rootly_incidents(es)
    events = query_rootly_events(es)
    messages = query_slack_messages(es)

    context = "Incident reports summary:\n"
    for incident in incidents:
        resolved_by = incident.get('resolved_by', 'Unknown')
        resolution_msg = incident.get('resolution_message', 'Unknown')
        context += f"- Incident ID {incident['id']} started at {incident['started_at']} with status {incident['status']}. Resolved by: {resolved_by} with the following resolution message {resolution_msg}\n"

    context += "\nIncident events summary:\n"
    for event in events:
        event_id = event.get('id', 'Unknown ID')  
        event_kind = event.get('kind', 'Unknown')  
        event_user = event.get('user_display_name', 'Unknown')  
        event_msg = event.get('event', 'Unknown')
        occurred_at = event.get('occurred_at', 'Unknown time')
        event_type = event.get('type', 'Unknown type')  
        context += f"- Event ID {event_id} occurred at {occurred_at} with type {event_type}, which was created by {event_user} containg the following message {event_msg}.\n"
    
    context += "\nRelevant Slack discussions:\n"
    for message in messages[:]:  # Limit to the last 5 messages for brevity
        message_text = message.get('text', 'Unknown')
        message_timestamp = message.get('timestamp', 'Unknown')
        message_user = message.get('user', 'Unknown')
        context += f"- Message: {message['text'][:]}...\n"  

    # Generate insights using OpenAI
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a helpful assistant knowledgeable about our incident reports, incident events, and Slack discussions."},
            {"role": "user", "content": context + question}
        ],
        temperature=0.7,
        max_tokens=4096,
        top_p=1.0,
        frequency_penalty=0.0,
        presence_penalty=0.0
    )

    try:
        answer = response.choices[0].message['content']
        return answer.strip()
    except KeyError:
        return "Failed to generate an answer."

incident_data = fetch_incident_from_rootly(incident_id)
if incident_data:
    index_incident_to_es(es, incident_data)

events_data = fetch_events_from_rootly(incident_id)
if events_data:
    index_events_to_es(es, events_data)

messages = fetch_slack_channel_history(channel_id)
if messages:
    preprocessed_messages = [preprocess_message_blocks(message) for message in messages]
    index_messages_to_es(es, preprocessed_messages)

question = "Give me a detailed summary of this incident, as well as the action itens and the list of people involved on it"
answer = ask_openai_about_incidents(question, es)
print(answer)
