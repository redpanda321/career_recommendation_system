from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes import auth, user, job, recommendation, chatbot, career_workspace
from dotenv import load_dotenv
import os

load_dotenv()

app = FastAPI(title="API Gateway")
app.router.redirect_slashes = True

allowed_origins = [
    origin.strip() for origin in os.getenv(
        "CAREER_ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8080"
    ).split(",") if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.get("/")
def service_running():
    return {"message":"Welcome to API Gateway"}

# Register microservice routes
app.include_router(auth.router, prefix="/auth")
app.include_router(user.router, prefix="/user")
app.include_router(recommendation.router, prefix="/recommend")
app.include_router(job.router, prefix="/jobs")
app.include_router(chatbot.router, prefix="/chatbot")
app.include_router(career_workspace.router, prefix="/career-workspace")
