from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Pilot View")


@app.get("/")
def home():
    return HTMLResponse("""
    <!doctype html>
    <html>
      <head>
        <title>Pilot View</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          body {
            margin: 0;
            min-height: 100vh;
            background:
              radial-gradient(circle at top left, rgba(56, 189, 248, 0.18), transparent 35%),
              radial-gradient(circle at bottom right, rgba(34, 197, 94, 0.12), transparent 35%),
              #020617;
            color: white;
            font-family: Arial, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
          }

          .panel {
            width: min(90vw, 650px);
            background: rgba(15, 23, 42, 0.86);
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 24px;
            padding: 32px;
            box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
          }

          .status {
            display: inline-block;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(34, 197, 94, 0.16);
            color: #86efac;
            font-weight: bold;
            letter-spacing: 0.08em;
            font-size: 12px;
          }

          h1 {
            margin: 18px 0 10px;
            font-size: 42px;
          }

          p {
            color: #cbd5e1;
            line-height: 1.55;
            font-size: 16px;
          }

          .grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
            margin-top: 24px;
          }

          .tile {
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 16px;
            padding: 16px;
          }

          .tile strong {
            display: block;
            margin-bottom: 6px;
          }

          .tile span {
            color: #94a3b8;
            font-size: 14px;
          }

          @media (max-width: 520px) {
            .grid {
              grid-template-columns: 1fr;
            }

            h1 {
              font-size: 34px;
            }
          }
        </style>
      </head>
      <body>
        <main class="panel">
          <div class="status">ONLINE</div>
          <h1>Pilot View</h1>
          <p>Garage vision server is running on the Raspberry Pi.</p>
          <p>The Gemini 2 camera layer is not connected yet. This page proves that SSH, Python, FastAPI, networking, and browser access are working.</p>

          <section class="grid">
            <div class="tile">
              <strong>Server</strong>
              <span>FastAPI running on port 8000</span>
            </div>
            <div class="tile">
              <strong>Camera</strong>
              <span>Awaiting Gemini 2</span>
            </div>
            <div class="tile">
              <strong>Mode</strong>
              <span>Local garage network</span>
            </div>
            <div class="tile">
              <strong>Next</strong>
              <span>Depth stream + parking overlay</span>
            </div>
          </section>
        </main>
      </body>
    </html>
    """)
