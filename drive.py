# drive.py — Google Drive upload + shareable link
import os, json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES    = ['https://www.googleapis.com/auth/drive']
FOLDER_ID = os.getenv('GOOGLE_DRIVE_FOLDER_ID')


def _service():
    # Prefer inline JSON string (Render env var) over a file path
    sa_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', '')
    if sa_json.strip().startswith('{'):
        info  = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # Fallback: treat value as a file path (local dev with credentials.json)
        path  = sa_json or 'credentials.json'
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds)


def upload_and_share(file_path: str) -> str:
    """Upload a .docx to Google Drive and return a shareable link."""
    svc   = _service()
    name  = os.path.basename(file_path)
    meta  = {'name': name, 'parents': [FOLDER_ID]}
    media = MediaFileUpload(
        file_path,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )
    file    = svc.files().create(body=meta, media_body=media, fields='id').execute()
    file_id = file['id']

    svc.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()

    return f'https://drive.google.com/file/d/{file_id}/view'
