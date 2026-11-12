"""รันเซิร์ฟเวอร์สำหรับพัฒนาในเครื่อง: python run.py"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
