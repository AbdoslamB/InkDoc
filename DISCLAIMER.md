# Disclaimer

This page explains what InkDoc does and does not promise, how third-party components fit in, and which features use the internet. It sits alongside InkDoc's [LICENSE](LICENSE) and does not replace or change it.

## No warranty

InkDoc is provided "as is", without warranty of any kind, as stated in the [MIT License](LICENSE). The license text is the authoritative statement of the warranty disclaimer and limitation of liability for InkDoc's own code. Some jurisdictions do not allow certain warranties or liabilities to be excluded, so parts of that disclaimer may not apply to you.

## Third-party components and models

InkDoc builds on software and AI models made by others, including Microsoft MarkItDown, Docling and its models, RapidOCR, GLM-OCR, llama.cpp, and Shift Labs' Markit. Each of these is governed by its own license and terms, which InkDoc's MIT License does not change. Some are under licenses other than MIT, for example CDLA-Permissive-2.0 for some Docling model weights, Apache-2.0 for others, and GPL-2.0 for the FLAC binaries that come with the SpeechRecognition package. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists every component, its license and how InkDoc distributes it; full license texts are in [LICENSES/](LICENSES/).

The optional engines and models (Docling, the Code & Formula Recognition add-on, GLM-OCR and the llama.cpp runtime) are provided by third parties. When you download one, you receive it under that third party's license and terms. Before you use any component, and especially before commercial use or redistribution, review its license yourself and make sure your use complies with it.

InkDoc's MIT License allows commercial use of InkDoc's own code. It does not extend to third-party components or models: you may use one of those commercially only if, and to the extent that, its own license allows it. Licenses can change between versions, so check the license of the exact version you use.

## No affiliation

InkDoc is an independent project. It is not affiliated with, endorsed by, or sponsored by Microsoft, IBM, the Docling project or the LF AI & Data Foundation, Z.ai (Zhipu AI), Shift Labs, Google, Hugging Face, or the llama.cpp / ggml project. All product names, logos and trademarks belong to their respective owners and are used only to identify the components InkDoc works with.

## Accuracy of output

Document conversion, OCR and AI-generated output can be incomplete or wrong. Text can be missed, misread or placed in the wrong order, tables and formulas can be reconstructed incorrectly, and transcripts can contain errors. InkDoc's missing-text check catches some of these problems, not all of them. Check the output before you rely on it, especially for legal, medical, financial or other important documents.

## Your content

You are responsible for the documents, URLs and media you convert. Make sure you have the right to process them (for example under copyright, contract or privacy law), and use InkDoc and its output lawfully. This includes the terms of any website or service whose content you convert.

## Network use and privacy

Most conversions run entirely on your computer. InkDoc's local server listens only on your own machine (`127.0.0.1`), and InkDoc's own code sends no usage analytics or telemetry. The features below do use the internet.

**Features that send your content to someone else**

| Feature | What leaves your computer | Who receives it |
| :--- | :--- | :--- |
| Audio transcription (WAV, MP3, M4A and MP4 files converted with MarkItDown, which is what Auto uses for audio) | The audio of the file | Google's speech-recognition service, used by MarkItDown through the SpeechRecognition package |
| Converting a web page URL | A request for that page, from your IP address | The website at that URL |
| Converting a YouTube link | Requests for the video page and its transcript | YouTube (Google) |

**Features that download software or check for updates**

| Feature | When | Contacts |
| :--- | :--- | :--- |
| Installing the Docling engine pack or the Code & Formula add-on | Only when you click Install in Settings | GitHub (InkDoc's releases) |
| Installing or updating GLM-OCR | Only when you confirm the download in Settings | Hugging Face (model) and GitHub (llama.cpp releases); InkDoc's GitHub release if those fail |
| GLM-OCR catalogue check | The first time you open Settings in a session, and when you start a GLM-OCR install or update | GitHub (InkDoc's releases) |
| App update check | When you click Check for updates, or once a day at startup if you turn on that setting (off by default) | GitHub (InkDoc's releases) |
| Markit engine on macOS or Linux with Node.js installed (on Windows, through WSL for some file types) | When a file is converted with Markit, including YAML and XML files under Auto | The npm registry, from which `npx` downloads @shiftlabs/markit. That package then runs on your computer with access to the file. |

**Other connections**

- **Fonts.** The interface loads the Inter and JetBrains Mono fonts from Google Fonts (`fonts.googleapis.com`, `fonts.gstatic.com`). This can cause the app to contact Google even when you have not converted a file. How often depends on caching.
- **Links you click**, such as the website link in Settings, open in your browser.

**Fully local**

Once installed, these work without the internet: converting local files with MarkItDown (except audio transcription), Docling, GLM-OCR, and Markit's built-in extractors; the missing-text check; the preview; and saving to your Downloads folder. After installation, Docling runs with Hugging Face downloads switched off, and GLM-OCR's `llama-server` runs offline, on your own machine only, with a per-session key.

If you run InkDoc from source with your own `pip install docling`, Docling may download its models from Hugging Face the first time it needs them.

The other companies and services named above have their own terms and privacy policies, and InkDoc does not control what they do with the data they receive.

## Not legal advice

InkDoc's documents, including this page, [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the README, are not legal advice. If you have legal questions about using InkDoc or any of its components, ask a qualified lawyer.
