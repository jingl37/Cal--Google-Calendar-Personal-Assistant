# Calendar Assistant backend

From the project root, start the API server with:

```bash
.venv/bin/python -m uvicorn chatbot:app --app-dir backend --host 127.0.0.1 --port 8000 --reload
```

Then start the frontend in a second terminal:

```bash
cd frontend && npm run dev
```

The frontend proxies `/api` requests to the backend on port 8000. Confirm that the API is available at `http://127.0.0.1:8000/test` before trying the chat UI.
