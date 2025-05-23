from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from trademe_service.scraper import run_scrape

app = FastAPI(title="Trade Me Waitakere API")


@app.get("/top")
async def top_listings(pages: int = Query(1, ge=1, le=10)):
    """
    Return listings from the specified number of pages (default 1, max 10).
    """
    try:
        data = await run_scrape(pages=pages)
        return JSONResponse(data)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape failed: {e}")
@app.get("/health")
def health():
    return {"status": "ok"}
