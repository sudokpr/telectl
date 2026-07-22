# OCR and Memory Code Flow

This document maps the source-code structure behind image ingestion, text
extraction, memory persistence, and memory queries.

Current runtime configuration:

- Codex image/text model: `gpt-5.4-mini`
- Ollama: disabled with `OLLAMA_ENABLED=false`
- Tesseract: final image-text fallback
- Memory storage: Markdown files under `MEMORY_WORK_DIR`

## Module dependencies

```mermaid
flowchart LR
    Telegram[Telegram API]
    CodexSDK[openai-codex SDK]
    Tesseract[Tesseract CLI]
    Ollama[Ollama HTTP API<br/>optional and currently disabled]
    Files[(Local filesystem)]

    Bot[bot.py<br/>handlers and orchestration]
    Image[image_summary.py<br/>image jobs and LLM routing]
    Memory[memory_processor.py<br/>persistence and retrieval]
    Spending[spending_index.py<br/>POI correlation and location context]
    Codex[codex_llm.py<br/>Codex SDK adapter]
    Metrics[metrics.py<br/>counters and timings]
    Backfill[backfill_image_memories.py<br/>one-time replay]

    Telegram --> Bot
    Bot --> Image
    Bot --> Memory
    Bot --> Spending
    Bot --> Metrics

    Image --> Codex
    Image --> Metrics
    Image --> Tesseract
    Image -. when enabled .-> Ollama
    Codex --> CodexSDK

    Memory --> Image
    Memory --> Files
    Spending --> Files
    Bot --> Files

    Backfill --> Bot
    Backfill --> Image
    Backfill --> Memory
```

`memory_processor.py` imports shared configuration and LLM/OCR functions from
`image_summary.py`. `image_summary.py` does not import `memory_processor.py`, so
this dependency does not form a circular import.

## Image upload: function-level call graph

```mermaid
flowchart TD
    subgraph bot.py
        Handler[image_summary_handler]
        Target[is_image_summary_target]
        Download[download_image]
        Reply[reply_chunked]
    end

    subgraph image_summary.py
        Jobs[image_result_jobs]
        Timed[timed_call]
        CodexVision[summarize_codex_vision]
        CodexImageChat[codex_image_chat]
        Compare[compare_ollama_to_codex]
        BuildReply[build_result_reply]
        OCR[run_ocr]
    end

    subgraph codex_llm.py
        AskImage[ask_codex_image]
        AskCodex[ask_codex]
    end

    subgraph memory_processor.py
        SaveImage[save_image_memory]
        HasMemory[has_image_memory]
        Digest[image_digest]
        Select[select_image_extraction]
        Save[save_memory]
        Extract[extract_memory]
        Markdown[memory_to_markdown]
    end

    Handler --> Target
    Target -->|accepted| Download
    Download --> Jobs
    Jobs --> Timed
    Timed --> CodexVision
    CodexVision --> CodexImageChat
    CodexImageChat --> AskImage
    AskImage --> AskCodex

    Timed --> BuildReply
    BuildReply --> Reply
    Handler --> Compare
    Handler --> SaveImage

    SaveImage --> HasMemory
    HasMemory --> Digest
    SaveImage --> Select
    Select -->|no usable model output| OCR
    Select -->|usable output| Save
    OCR -->|usable raw text| Save

    Save --> Extract
    Extract -->|structured metadata| Markdown
    Extract -->|LLM failure: raw fallback| Markdown
    Markdown --> MemoryFile[(MEMORY_WORK_DIR/*.md)]
```

## Image upload: runtime sequence

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Telegram
    participant Bot as bot.image_summary_handler
    participant Image as image_summary
    participant Codex as codex_llm
    participant Memory as memory_processor
    participant Disk as Local filesystem

    User->>Telegram: Upload photo or image document with optional caption
    Telegram->>Bot: Update
    Bot->>Bot: is_image_summary_target
    Bot->>Telegram: Received image; processing
    Bot->>Disk: Download original image

    Bot->>Image: image_result_jobs(image_path, cfg, message.caption)
    Image->>Codex: ask_codex_image(prompt plus user comment)
    Codex-->>Image: Extracted rows, labels and facts
    Image-->>Bot: timed result dictionary
    Bot->>Telegram: Send extraction result

    Bot->>Memory: save_image_memory(image_path, results, cfg, source)
    Memory->>Disk: Calculate SHA-256 and scan existing memories

    alt image hash and same comment already stored
        Memory-->>Bot: None
        Bot->>Disk: Log image_memory_skipped
    else new image or new correction comment
        Memory->>Memory: select_image_extraction
        Memory->>Image: text_llm_chat(memory structuring prompt)
        Image->>Codex: ask_codex_text
        Codex-->>Image: JSON title, category, summary, fields and tags
        Image-->>Memory: Structured JSON text
        Memory->>Memory: memory_to_markdown
        Memory->>Disk: Write or update Markdown with full Raw Text and comment
        Memory-->>Bot: SavedMemory
        Bot->>Telegram: Saved extracted image memory
    else no usable model extraction
        Memory->>Image: run_ocr(image_path, cfg)
        Image-->>Memory: Raw Tesseract text
        Memory->>Memory: save_memory
        Memory->>Disk: Write Markdown memory
        Memory-->>Bot: SavedMemory
        Bot->>Telegram: Saved extracted image memory
    end
```

## Memory construction internals

```mermaid
flowchart TD
    Raw[Extracted image text<br/>or plain Telegram text]
    Save[save_memory]
    Extract[extract_memory]
    TextRouter[text_llm_chat]
    CodexText[codex_text_chat]
    Parse[parse_memory_json]
    Fallback[fallback_memory]
    Render[memory_to_markdown]
    Slug[slugify title]
    File[(timestamp-title.md)]

    Raw --> Save
    Save --> Extract
    Extract --> TextRouter
    TextRouter -->|CODEX_LLM_ENABLED| CodexText
    CodexText -->|valid response| Parse
    TextRouter -. optional fallback when enabled .-> OllamaText[ollama_chat]
    Parse -->|valid JSON| Render
    Parse -->|invalid JSON| Fallback
    CodexText -->|request failure| Fallback
    Fallback --> Render
    Save --> Slug
    Render --> File
    Slug --> File

    subgraph Markdown contents
        Frontmatter[Frontmatter<br/>category, tags, source, created_at]
        Body[Body<br/>title, summary, key fields, Raw Text]
    end

    Render --> Frontmatter
    Render --> Body
```

## Query path

```mermaid
flowchart TD
    subgraph bot.py
        Command[memory_query_command<br/>/memq question]
        Prefix[memory_query_text_handler<br/>? question]
        AnswerHandler[answer_memory_query]
    end

    subgraph memory_processor.py
        Answer[answer_memory_question]
        Relevant[relevant_memories]
        Terms[query_terms]
        Score[memory_score]
        Context[build_memory_context]
    end

    subgraph image_summary.py
        TextLLM[text_llm_chat]
    end

    subgraph codex_llm.py
        CodexText[codex_text_chat]
        AskText[ask_codex_text]
        Ask[ask_codex]
    end

    Command --> AnswerHandler
    Prefix --> AnswerHandler
    History[Last 3 query turns<br/>same user, chat and topic] --> AnswerHandler
    AnswerHandler --> Answer
    Answer --> Relevant
    Relevant --> Terms
    Relevant --> Score

    Score --> Ranked[Sort by score and modification time]
    Ranked --> TopK[Keep MEMORY_QUERY_TOP_K]
    TopK --> Context
    History --> Context
    POILinks[spending.memory_poi_context<br/>saved deterministic links] --> Context
    Context --> TextLLM
    TextLLM --> CodexText
    CodexText --> AskText
    AskText --> Ask
    Ask --> Response[Answer plus source filenames]
```

The query scorer searches exact lowercase alphanumeric tokens across both the
filename and complete Markdown content. It rewards occurrence count, number of
distinct query terms covered, complete multi-term coverage, and filename
matches. It does not currently use embeddings or a separate metadata index.

## Backfill path

```mermaid
flowchart TD
    Main[backfill_image_memories.main]
    Paths[image_paths]
    Backfill[backfill]
    Check[has_image_memory]
    Jobs[image_result_jobs]
    Save[save_image_memory]

    Main --> Paths
    Paths --> Backfill
    Backfill --> Check
    Check -->|hash exists| Skip[Skip image]
    Check -->|missing| Jobs
    Jobs --> Save
    Save -->|saved| Continue[Process next image]
    Save -->|duplicate hash found during run| Skip
```
