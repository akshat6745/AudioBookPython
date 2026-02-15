# Deployment Guide for AudioBookPython

This guide outlines the steps to deploy the AudioBookPython application to [Render](https://render.com) using Docker.

## Prerequisites

Before you begin, ensure you have the following:

1.  **GitHub Account**: The code must be in a GitHub repository.
2.  **Render Account**: Sign up at [render.com](https://render.com).
3.  **Supabase Credentials**: You will need your `SUPABASE_URL` and `SUPABASE_KEY` (service role or anon key, depending on your RL policies, but usually service role for backend operations).
4.  **Google Sheet ID**: The ID of the Google Sheet used for novel management.

## Deployment Steps

### 1. Push Code to GitHub

Ensure your latest code, including the `render.yaml` and `Dockerfile`, is pushed to your GitHub repository.

```bash
git add .
git commit -m "Prepare for Render deployment"
git push origin main
```

### 2. Create a New Blueprint Instance on Render

1.  Log in to your Render dashboard.
2.  Click on the **"New +"** button and select **"Blueprint"**.
3.  Connect your GitHub account if you haven't already.
4.  Select the `AudioBookPython` repository.

### 3. Configure Environment Variables

Render will detect the `render.yaml` file and prompt you to input the values for the environment variables defined in it.

You will see fields for:

*   `SHEET_ID`: Enter your Google Sheet ID.
*   `SUPABASE_URL`: Enter your Supabase Project URL.
*   `SUPABASE_KEY`: Enter your Supabase API Key.

**Note**: `ENVIRONMENT` is automatically set to `production` and `LOG_LEVEL` to `INFO` by the blueprint.

### 4. Deploy

1.  Click **"Apply"** or **"Create Blueprint"**.
2.  Render will start building your Docker container. This may take a few minutes as it installs dependencies including `ffmpeg`.
3.  Watch the logs for any errors.
4.  Once the build finishes, the service will start, and you will see a green "Live" badge.

## Verification

### 1. Check the Health Endpoint

Visit your new Render URL (e.g., `https://audiobook-python.onrender.com/health`).
You should see a JSON response with status `"healthy"`.

### 2. Test Functionality

You can test the API using `curl` or a tool like Postman.

**Example: List Novels**
```bash
curl https://your-app-name.onrender.com/novels
```

## Troubleshooting

-   **Build Fails**: Check the "Logs" tab in the Render dashboard. A common issue is missing dependencies in `requirements.txt`.
-   **Service Won't Start**: Ensure `gunicorn` and `uvicorn` are in `requirements.txt`. Check if the `PORT` environment variable is being picked up (the Dockerfile handles this).
-   **502 Bad Gateway**: The application might be taking too long to start or crashing immediately. Check the logs.
