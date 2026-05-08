# drive.py — Google Drive upload + shareable link
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES           = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCT_JSON = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', 'credentials.json')
FOLDER_ID        = os.getenv('GOOGLE_DRIVE_FOLDER_ID')


def _service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCT_JSON, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=creds)


def upload_and_share(file_path: str) -> str:
    """Upload a .docx to Google Drive and return a shareable link."""
    svc  = _service()
    name = os.path.basename(file_path)
    meta = {'name': name, 'parents': [FOLDER_ID]}
    media = MediaFileUpload(
        file_path,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )
    file = svc.files().create(body=meta, media_body=media, fields='id').execute()
    file_id = file['id']

    # Make it readable by anyone with the link
    svc.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()

    return f'https://drive.google.com/file/d/{file_id}/view'
