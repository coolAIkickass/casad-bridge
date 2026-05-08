# whatsapp.py — WhatsApp message parsing, media download, reply sending
import os, requests

TOKEN         = os.getenv('WHATSAPP_TOKEN')
PHONE_NUM_ID  = os.getenv('PHONE_NUMBER_ID')
MEDIA_DIR     = os.getenv('MEDIA_DIR', 'media')
BASE_URL      = f"https://graph.facebook.com/v19.0/{PHONE_NUM_ID}"

os.makedirs(MEDIA_DIR, exist_ok=True)


def parse_payload(data: dict) -> dict:
    """Extract the relevant fields from a WhatsApp webhook POST body."""
    try:
        entry   = data['entry'][0]['changes'][0]['value']
        message = entry['messages'][0]
        phone   = message['from']
        msg_id  = message['id']
        mtype   = message['type']

        content  = None
        media_id = None

        if mtype == 'text':
            content = message['text']['body']
        elif mtype == 'audio':
            media_id = message['audio']['id']
        elif mtype == 'image':
            media_id = message['image']['id']
            content  = message['image'].get('caption', '')

        return {
            'phone':    phone,
            'msg_id':   msg_id,
            'type':     mtype,
            'content':  content,
            'media_id': media_id,
            'seq':      0,
        }
    except (KeyError, IndexError) as e:
        return {'phone': 'unknown', 'type': 'unknown', 'content': None, 'seq': 0}


def download_media(media_id: str, save: bool = False) -> bytes | str:
    """Download a media file from WhatsApp. Returns bytes or saved file path."""
    headers = {'Authorization': f'Bearer {TOKEN}'}
    # Step 1: get the download URL
    url_resp = requests.get(
        f'https://graph.facebook.com/v19.0/{media_id}', headers=headers
    )
    url_resp.raise_for_status()
    download_url = url_resp.json()['url']

    # Step 2: download the bytes
    media_resp = requests.get(download_url, headers=headers)
    media_resp.raise_for_status()
    raw = media_resp.content

    if save:
        ext = url_resp.json().get('mime_type', 'image/jpeg').split('/')[1]
        path = os.path.join(MEDIA_DIR, f'{media_id}.{ext}')
        with open(path, 'wb') as f:
            f.write(raw)
        return path

    return raw


def send_message(phone: str, text: str) -> None:
    """Send a text reply to a WhatsApp number."""
    headers = {
        'Authorization': f'Bearer {TOKEN}',
        'Content-Type':  'application/json',
    }
    payload = {
        'messaging_product': 'whatsapp',
        'to':   phone,
        'type': 'text',
        'text': {'body': text},
    }
    r = requests.post(f'{BASE_URL}/messages', json=payload, headers=headers)
    r.raise_for_status()
