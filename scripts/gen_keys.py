"""สร้างค่า SECRET_KEY, FILE_ENCRYPTION_KEY, SETUP_TOKEN, CRON_SECRET สำหรับใส่ใน Vercel"""
import secrets

from cryptography.fernet import Fernet

print("SECRET_KEY=" + secrets.token_urlsafe(48))
print("FILE_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
print("SETUP_TOKEN=" + secrets.token_urlsafe(24))
print("CRON_SECRET=" + secrets.token_urlsafe(32))
print()
print("# แปลงไฟล์ service account เป็น base64 (ใส่ใน FIREBASE_CREDENTIALS):")
print("#   python -c \"import base64;print(base64.b64encode(open('serviceAccount.json','rb').read()).decode())\"")
