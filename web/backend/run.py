"""Run the backend: python run.py  (or: uvicorn app.main:app --reload)"""
import uvicorn

from app import config

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT, reload=False)
