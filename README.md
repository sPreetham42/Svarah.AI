# Svarah.ai — AI-Powered Marketing Campaigns

**Turn a shopkeeper’s voice into a complete, multilingual marketing campaign.**

Svarah.ai is an AI-powered marketing platform designed to help small businesses create promotional content without needing marketing expertise, design skills, or professional production tools.

Describe your business and offer using your voice or text. Svarah.ai transforms that information into a structured campaign, generates promotional content, creates voiceovers in selected languages, and prepares the final assets for publishing through Upload-Post.

##  Features

* **Voice-first campaign creation** — capture business details and offers through speech or typed input.
* **AI campaign planning** — use Agnes AI to understand the business, extract key facts, and plan campaign content.
* **Fact integrity** — review and lock important business facts before generating marketing assets.
* **AI copywriting** — generate promotional text, captions, and campaign variations.
* **Poster generation** — create campaign visuals using Agnes image generation.
* **Promotional video generation** — generate video assets through the configured Agnes video workflow.
* **Multilingual campaigns** — prepare campaign content for selected languages.
* **AI voiceovers** — use Sarvam AI text-to-speech to produce spoken promotional content, including language-specific audio variants.
* **Asset review** — inspect generated campaign materials before publishing.
* **Social publishing integration** — use Upload-Post to connect supported social platforms and publish eligible campaign content.
* **Mock and live modes** — develop and test supported workflows without requiring every external provider to be available.

##  The Problem

Small business owners often lack the time, tools, and expertise to create professional marketing campaigns. Producing a poster, writing captions, recording an advertisement, translating content, and publishing across social platforms can require several disconnected tools.

Svarah.ai brings these steps into one campaign workflow.

##  How It Works

```text
Business Owner
      │
      ▼
Voice or Text Input
      │
      ▼
Sarvam AI Speech-to-Text
      │
      ▼
Agnes AI — Business Fact Extraction
      │
      ▼
Review and Lock Business Facts
      │
      ▼
Agnes AI — Campaign Planning
      │
      ▼
Generate Campaign Assets
      ├── Promotional Copy
      ├── Posters and Images
      ├── Promotional Videos
      └── Multilingual Voiceovers
                │
                ▼
      Review Final Campaign
                │
                ▼
      Upload-Post Publishing
```

The precise availability of each asset type depends on the configured providers, account permissions, and successful generation jobs.

##  Technology Stack

| Layer                 | Technology                                          |
| --------------------- | --------------------------------------------------- |
| Frontend              | React, JavaScript, Vite                             |
| Backend               | Python, FastAPI                                     |
| Campaign intelligence | Agnes AI — Agnes 3.0 Flash                          |
| Image generation      | Agnes AI image model                                |
| Video generation      | Agnes AI video model                                |
| Speech-to-text        | Sarvam AI — Saaras                                  |
| Text-to-speech        | Sarvam AI — Bulbul                                  |
| Database              | SQLite in the current local configuration           |
| Asset storage         | Local filesystem in the current local configuration |
| Social publishing     | Upload-Post                                         |
| Testing               | Pytest and frontend build checks                    |

The project uses external AI services for generation and speech processing. API credentials and account quotas determine whether live operations can execute successfully.

##  Architecture

Svarah.ai separates the user interface, campaign orchestration, provider integrations, and asset handling.

### Frontend

The React application provides the campaign creation experience, fact review, planning, asset generation, voiceover review, and publishing workflow.

### Backend

The FastAPI application coordinates campaign operations, validates inputs, manages provider requests, handles generation status, and exposes the APIs consumed by the frontend.

### Campaign intelligence

Agnes AI acts as the campaign director. It interprets business information, helps structure the campaign, and coordinates the content-generation workflow.

### Fact integrity

Important business information—such as prices, discounts, product names, and offer conditions—should be reviewed before it is used to produce campaign assets. Creative variations must not silently change the underlying offer.

### Media generation

Dedicated services handle text, images, videos, and speech. Long-running generation tasks may require polling and explicit handling of pending, failed, and completed states.

### Publishing

Upload-Post handles the configured social publishing integration. Campaign assets should be reviewed before the existing publishing workflow is used. Platform support and publishing permissions depend on the connected accounts and Upload-Post configuration.

## 🎙️ Multilingual Voiceovers

Svarah.ai uses Sarvam AI for speech recognition and speech synthesis.

The voiceover workflow is intended to:

1. Obtain the approved campaign script.
2. Identify the languages selected for the campaign.
3. Prepare the appropriate language-specific script.
4. Generate audio using Sarvam AI's supported text-to-speech capabilities.
5. Associate each audio file with its language and campaign.
6. Allow the owner to play and review the generated audio.
7. Present the final voiceover assets before the existing publishing step.

Each language variant should be handled independently so a failure in one does not incorrectly mark every variant as successful. Unsupported languages, provider errors, and missing audio should be reported clearly.

**Speech provider policy:** Sarvam AI is the speech-to-text and text-to-speech provider. No alternate speech provider should be silently substituted.

## 🔒 Fact Integrity and Owner Control

Svarah.ai is designed around a simple principle:

> **AI can create the marketing. The business owner controls the truth.**

The workflow separates business facts from creative generation:

* Extract and review important facts.
* Lock the approved offer information.
* Generate campaign content using the approved information.
* Validate content where supported by the application.
* Let the owner review the final assets.
* Proceed to publishing through the existing publishing integration.

AI-generated content should not be treated as verified merely because generation succeeded.

## 🚀 Getting Started

### Prerequisites

Install the following:

* Python 3.11 or a compatible version supported by the project's dependencies.
* Node.js and npm.
* Git.
* API credentials for the external providers you intend to use.

### 1. Clone the repository

```bash
git clone https://github.com/SwarupRock/friendly-octo-fishstick.git
cd friendly-octo-fishstick
```

### 2. Configure the backend

Create and activate a virtual environment from the repository root:

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the backend dependencies using the repository's dependency file. For example, if it contains `backend/requirements.txt`:

```powershell
pip install -r backend/requirements.txt
```

Create your local environment file from the supplied template:

```powershell
Copy-Item backend/.env.example backend/.env
```

If the repository keeps its environment template at the root instead, use that template and follow its documented paths.

Configure the required variables in the actual environment template. Typical provider configuration includes:

* Agnes API credentials and model settings.
* Sarvam API credentials and speech model settings.
* Application mode and storage settings.
* Database configuration.
* Upload-Post credentials and publishing settings, where required.

**Do not commit `.env` files or real API keys.** Use placeholders in examples and configure production secrets through the deployment environment.

### 3. Start the backend

From the backend directory, if the application entry point is `app/main.py`:

```powershell
cd backend
uvicorn app.main:app --reload
```

The backend's interactive API documentation is normally available at:

`http://127.0.0.1:8000/docs`

If the repository defines a different entry point or port, use the configuration and scripts supplied with the project.

### 4. Start the frontend

Open another terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open the local URL printed by Vite in the terminal.

### 5. Configure live providers

Before using live generation or publishing:

1. Add the required provider credentials to the local environment.
2. Confirm the application is configured for live mode.
3. Check that the provider account has sufficient quota and permissions.
4. Verify the backend can reach the configured APIs.
5. Test generation and playback before attempting a live publishing operation.

The exact environment variable names and defaults in the repository's current configuration take precedence over any examples in this README.

## ⚙️ Configuration

Configuration is environment-driven. Consult the supplied environment template and backend settings for the authoritative variable names.

| Configuration area | Purpose                                              |
| ------------------ | ---------------------------------------------------- |
| Application mode   | Select mock or live provider behavior                |
| Database           | Configure the application database                   |
| Asset storage      | Set the location for generated assets                |
| Agnes AI           | Configure campaign intelligence and media generation |
| Sarvam AI          | Configure speech recognition and synthesis           |
| Generation limits  | Control supported campaign asset quantities          |
| Background jobs    | Configure the task execution mode                    |
| Upload-Post        | Configure social account connections and publishing  |

Keep credentials server-side. Never expose provider secrets in frontend code, public API responses, logs, screenshots, or committed files.

## 🧪 Testing and Verification

Run the tests using the commands and test configuration defined by the repository.

Typical checks include:

```powershell
# Backend tests
pytest
```

```powershell
# Frontend production build
cd frontend
npm run build
```

Verify the following workflows before release:

* Campaign creation from voice and typed input.
* Speech transcription and fact extraction.
* Fact review and locking.
* Campaign planning and content generation.
* Poster and video generation, including asynchronous job status.
* Voiceover generation, playback, and error handling.
* Audio generation for every supported selected language.
* Final campaign review before publishing.
* Upload-Post publishing behavior for configured platforms.
* Failure handling when credentials, quota, or provider responses are unavailable.

Mock-mode tests do not prove that a live provider integration works. Live verification should be reported separately and performed only when valid credentials and sufficient quota are available.

##  Known Operational Considerations

* AI generation can fail because of provider errors, rate limits, or insufficient account quota.
* Video generation may take longer than text, image, or speech operations.
* Each selected language must be supported by the configured Sarvam capabilities and voice parameters.
* Translation and speech synthesis are separate operations; successful generation of source-language audio does not prove that translated audio exists.
* Social publishing depends on account authorization, platform restrictions, and the configured Upload-Post integration.
* The current local SQLite and filesystem setup should not automatically be treated as a production-ready multi-user deployment.
* Production deployment requires appropriate authentication, access controls, secret management, durable storage, and operational monitoring.

##  Product Workflow

The core product workflow is:

1. Capture business information.
2. Extract and review facts.
3. Lock the approved business information.
4. Plan the campaign.
5. Generate promotional copy and media.
6. Prepare voiceovers for selected languages.
7. Review the completed campaign.
8. Publish through Upload-Post or use the available export workflow.

## Security

* Keep all API credentials in environment variables or a secure secret manager.
* Never commit local environment files containing credentials.
* Enforce authorization and campaign ownership on protected backend operations.
* Validate inputs and handle provider responses safely.
* Avoid logging sensitive customer data or secrets.
* Do not report an asset as successful unless the required output was actually generated and stored.
* Require appropriate user authorization before publishing content to connected social accounts.

## Project Status

Svarah.ai brings together campaign planning, content generation, multilingual speech, and social publishing in a unified workflow.

The live availability of individual features depends on the current implementation, provider credentials, account permissions, and successful end-to-end verification. Consult the repository's tests and latest verification results before making claims about production readiness.

##  License

Add the repository's intended license here. Until a license is included, do not assume the source code is licensed for unrestricted reuse or redistribution.

---

**Svarah.ai — From a shopkeeper's voice to a campaign ready for the world.**
