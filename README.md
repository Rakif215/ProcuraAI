# ProcuraAI

> AI-powered RFQ (Request for Quotation) automation platform for procurement teams.

## Project Structure

```
ProcuraAI/
├── frontend/          # React 19 + Vite + TypeScript (Aura Glass UI)
├── backend/           # FastAPI + Python (RFQ pipeline, AI agents, auth)
├── docs/              # Planning documents, sprint tracking, epics
│   └── planning/
│       ├── epics.md          # Product roadmap — Epics 4–10
│       └── sprint-status.yaml # Live sprint tracking
├── .github/
│   └── workflows/
│       └── ci.yml     # GitHub Actions — backend check + frontend build
├── vercel.json        # Vercel deployment config (frontend)
├── netlify.toml       # Netlify deployment config (frontend, alternative)
└── .gitignore
```

## Quick Start

### Backend (FastAPI)
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your Supabase + AI keys
python run.py
# → http://localhost:8000
```

### Frontend (React + Vite)
```bash
cd frontend
npm install
cp .env.example .env.local   # set VITE_API_URL=http://localhost:8000/api
npm run dev
# → http://localhost:3000
```

## Deployment

| Layer | Platform | URL |
|-------|----------|-----|
| Frontend | Vercel (free) | `https://procura-ai.vercel.app` |
| Backend | Render (free) | `https://procuraai-api.onrender.com` |
| Database | Supabase (free tier) | Managed |

## Environment Variables

### Frontend (`frontend/.env.local`)
```
VITE_API_URL=http://localhost:8000/api   # dev
VITE_API_URL=https://procuraai-api.onrender.com/api   # production (set in Vercel dashboard)
```

### Backend (`backend/.env`)
See `backend/.env.example` for full list. Key variables:
- `SUPABASE_URL`, `SUPABASE_KEY`
- `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`
- `FERNET_KEY` (for encrypting email credentials)

## Tech Stack

- **Frontend**: React 19, TypeScript, Vite, Framer Motion, Recharts, Lucide Icons
- **Backend**: FastAPI, Python 3.11, LangGraph, Supabase (PostgreSQL + RLS)
- **AI**: Groq → OpenRouter → Gemini (cascading fallback)
- **Auth**: Supabase Auth + JWT
- **PDF**: ReportLab
- **Testing**: Playwright (E2E), pytest (backend)
