# PROMPTS

How I used AI to build Stark Translator.

## Summary

1. **Product description.** I started from the product description and wrote it down as the project requirements (`.ai/specs/document_translation.md`).
2. **Plan.** I had an AI draft the implementation plan from that description.
3. **Plan correction with Claude.** I reviewed the plan with Claude and corrected it: architecture, scope, pipeline stages, and the cost and concurrency model.
4. **Scaffold with Cursor.** I built the first working version with Cursor from the corrected plan: backend, worker, formats, frontend, MCP server, and compose.
5. **Agent check and fixes.** I had Claude Code agents audit the build against the requirements, write a QA plan, run it, and fix the main issues they found.
6. **Manual check, architecture correction, polishing.** I tested the app and the MCP server by hand. For each bug or design smell I wrote down what I saw (`.ai/specs/hand-check-*.md`), had Claude turn it into plans split so parallel agents never touch the same file (`.ai/plans/`), and corrected the architecture step by step. Main changes from this stage:
   - Real use of `openai-agents` for the review stage, not a nominal wrapper.
   - MCP moved to Streamable HTTP so it starts with `docker compose up`.
   - Async MCP tools so one translation never blocks another.
   - Redis pub/sub plus a separate realtime SSE service, so live progress does not hold Postgres connections.
   - User accounts, per-user history and translation memory, API tokens for MCP.
   - MCP as an independent service: signed upload and download links and `list_documents`, no host paths over HTTP.

### What I rejected

- Postgres `LISTEN/NOTIFY` with SSE served by the API for live progress. It does not scale to many open browsers, because every stream ties up the database. I replaced it with Redis pub/sub and a separate realtime service.
- Sync MCP tools that poll in a thread. I chose async tools with SSE push.
- `openai-agents` used only by name, with a plain function as the "guardrail". I asked for it to be used properly or not at all.
- A shared history and a global translation memory. I chose per-user data.
- `MCP_WORKSPACE` and `path` as the main way to send files. A remote client does not know the server's folders, so over HTTP the tools use upload links and `document_id` instead.

## Prompt log

Times are local (EET, UTC+2). Cursor and Claude Code are in one table, in the order each prompt was sent. Answers that changed a plan are included. Slash-only commands are left out. Briefs the parent agent wrote for its own subagents are left out; the prompt that started that run is kept. An API token pasted into one prompt is omitted here.

| Date and time | Tool | Prompt |
| --- | --- | --- |
| 2026-11-05 11:31 | Cursor | There is document_translation.md specification for the new project. Create a plan how to build service and also create a plan for QA agent how to test described edge cases. |
| 2026-11-05 11:35 | Cursor | I'm not sure what you mean Postgres for job queue, explain why it is better than Redis + Message broker like RabbitMQ. |
| 2026-11-05 11:42 | Cursor | ok, but we also can add some state to the rabbit mq + redis, only one dissadvantage is when redis will be down. Explore approach to keep state on Redis when service restarting. |
| 2026-11-05 12:22 | Cursor | how about chunking? Chunk: group blocks into segments of ~1500 tokens on block boundaries; src_hash per segment. I mean if I select another translation model with another context window I would like to have some mapping of the available options and then correct chuncking. |
| 2026-11-05 12:29 | Cursor | how about parallelism, how many chunks I can run at a time for openAI API? is that can be done by batch call to OpenAI for the whole 200 pages doc or must be split by few calls? |
| 2026-11-05 13:09 | Cursor | add uv file inside tmp folder to able to run that script @tmp/list_of_openai_models.py and @tmp/check_tpm.py |
| 2026-11-05 13:19 | Cursor | I have gpt-4o models access only. I checked with script. So planning only with the best gpt-4o option. It must be gpt-4o-mini as gpt-4o costly |
| 2026-11-05 13:21 | Cursor | here is the correct output (models script terminal) |
| 2026-11-05 13:24 | Cursor | how about scanned documents or photos is gpt-4o-mini supports it? |
| 2026-11-05 13:32 | Cursor | is there any router or guardrail cheap model that decide what is the document came? because it can be just plain pdf, pdf with pure quality photos and photos with differrent quality. |
| 2026-11-05 13:38 | Cursor | yes add it to TODO. also there can be a case when I upload book for example and it can contain images together with text, so it is looks like clasifier must check every page and also keep original images when translating books or articles. |
| 2026-11-05 13:43 | Cursor | is it makes sense to add langGraph to have separated steps that will be good for log review on LangSmith? check also for alternative logging approach where I can quickly check which step eats most tokens and can quickly adjust it |
| 2026-11-05 13:51 | Cursor | what "OpenAI's own Traces dashboard" shows? Can I see the same as on plain Posgress data or Langfuse or Graphana dashboard? |
| 2026-11-05 13:59 | Cursor | so, shortly you propose to keep Postgres provider_calls for sure with Graphana and add Langfuse as optional? |
| 2026-11-05 14:00 | Cursor | ok, add it to plan |
| 2026-11-05 14:44 | Cursor | what is "Branded for Stark (find the branding online)" is it specific type of style? |
| 2026-11-05 14:57 | Cursor | check and add to plan https://brandfetch.com/starkfuture.com design colors if needed. |
| 2026-11-05 14:59 | Cursor | how to increase md file font on the terminal? |
| 2026-11-05 15:06 | Cursor | Document Translator: Build Plan + QA Agent Plan. Implement the plan. |
| 2026-11-05 17:36 | Cursor | I had 22 todos on a plan document_translator_build plan why only 15 completed? |
| 2026-11-05 17:38 | Cursor | but it was on the session build, I've seen update plan file and 've seen 22 todos and click build on tha file, why not all todos were build and not marked as done? |
| 2026-11-05 17:40 | Cursor | complete remaining todos from the document_translator_build plan |
| 2026-11-05 17:50 | Cursor | push initial changes to git@github.com:begin29/document_translator.git |
| 2026-11-05 18:00 | Cursor | you are QA, read the build plan as what was done and document_translation.md as a requirements. also there are QA notes @docs/QA_PLAN.md what must be checked. Test application and write a result to @docs/QA_RESULTS.md under new section at the end of the file, don't override that file and check what is working, what is not. |
| 2026-11-05 18:07 | Cursor | keep only OPENAI_API_KEY, remove OPEN_AI_API_KEY ussage |
| 2026-11-05 18:10 | Cursor | what is the minimum needed on the .env to run qa with real openAI API ? |
| 2026-11-05 18:14 | Cursor | you are senior QA, read the build plan as what was done and document_translation.md as a requirements. also there are QA notes. You already have an access to real openAI API, test it carefully. Test application and write a result to @QA_RESULTS.md under new section at the end of the file, don't override that file and check what is working, what is not. |
| 2026-11-05 18:38 | Cursor | how to run Graphana? |
| 2026-11-05 18:42 | Cursor | ignore @docs/ folder as it is temporary and push the changes |
| 2026-11-05 18:45 | Cursor | how to allow search and add to agent window gitignored files on Cursor? |
| 2026-11-05 18:50 | Cursor | check the spec and QA_RESULTS.md and plan to fix: scanned pages and photos classed blank so OCR never runs; a finished 200-page Italian job only partly translated, with the price card not matching the job row; review can spend a gpt-4o call and still save English; jobs stick in ocr and reviewing. |
| 2026-11-05 18:59 | Cursor | Fix scanned pages classed blank. Implement the plan. |
| 2026-11-05 19:07 | Cursor | recent changes must fix the QA findings (scans classed blank, partial Italian job, cost mismatch, review keeping English, jobs stuck in ocr and reviewing). Retest. |
| 2026-11-05 19:45 | Cursor | test MCP connection and files translation using current Cursor agent |
| 2026-11-05 20:16 | Cursor | have you checked all available mcp endpoints? |
| 2026-11-06 10:22 | Cursor | read the build plan and codebase and create AGENTS.md file on the root folder that different agents can read. It must not be more than 200 lines and to be usefull as it will be loaded all the time agent will run |
| 2026-11-06 10:37 | Cursor | I have .ai folder with specs. Propose how to keep structure there. I have tmp/document_translation.md file as a main starting point but later I would like to add additional specs while working on some feature. Is there any INDEX.md needed? |
| 2026-11-06 11:58 | Claude Code | read @.ai/specs/document_translation.md it is project requirements. check if everything was done, create QA plan and test application. you can also check @.ai/specs/QA_PLAN.md for some inspiration, but not neccecary. |
| 2026-11-06 12:01 | Cursor | why claude tried to delete some folder running a scratchpad clone (rm -rf scratchpad/clone && git clone the project && start a throwaway postgres)? |
| 2026-11-06 12:09 | Cursor | what is claude scratchpad? |
| 2026-11-06 13:40 | Claude Code | connect to MCP using @README.md and check methods |
| 2026-11-06 13:44 | Cursor | is it good practice to run MCP together with API? |
| 2026-11-06 14:12 | Cursor | what to write to @.cursorignore to have files and folders inside docs visible for file opener? |
| 2026-11-06 14:36 | Cursor | where is max file size is set? |
| 2026-11-06 14:40 | Cursor | where to put logic that drives claude code or cursor to use @.ai/specs as a folder where to put planned work? |
| 2026-11-06 14:43 | Cursor | no, only during current build I should see some spec, but there can be working few agents on the same problem in parallel like subagents. |
| 2026-11-06 14:46 | Cursor | how about tell agent to read for example @.ai/specs/hand-check-06-11-2026-13-30.md and create separate issues for subagents to run in parallel? |
| 2026-11-06 14:50 | Cursor | how to allow claude code to see some .gitignore files? |
| 2026-11-06 14:51 | Cursor | add that to AGENTS.md. Untracked plans for the current build are in .ai/specs/. If that folder has files, read the file named in your task before editing |
| 2026-11-06 14:54 | Cursor | when I put both .ignore and .gitignore which one will be used by ClaudeCode? |
| 2026-11-06 15:00 | Claude Code | /plan read .ai/specs/hand-check-06-11-2026-13-30.md and create a plan for every change I found. make it in parallel if that is possible as it 2 agents won't touch one file. Put plan to the .ai/plans folder where Cursor or Codex can grab and build. |
| 2026-11-06 16:39 | Cursor | why I might need "OpenAI Agents SDK" here on the document translator ? I mean on which stage it can be used? |
| 2026-11-06 16:44 | Cursor | but is it used OpenAI Agents SDK anywhere? I can't see it is connected 'openai-agents' library anywhere |
| 2026-11-06 16:50 | Claude Code | Answers. How should the MCP service reach the user's files? → "Mounted folder". What should happen to the nominal `openai-agents` review agent? → "Use it properly". |
| 2026-11-06 17:09 | Claude Code | Rejected writing `00-index.md` and stopped the agent to discuss the plan first. |
| 2026-11-06 17:12 | Claude Code | /plan read that plan on .ai/plans/lovely-churning-boot.md and tell me next: where did you find that MCP polling with `anyio.sleep`. `HttpClient` is synchronous (blocking `httpx.Client` plus `time.sleep`). That answers spec question 5: `translate` is sync. `__main__.py` runs it in a thread and has a watcher poll a shared dict every 0.2 s. so is it sync or async? I need to do translations async as translate one document can blocks translation another. Let's chat about that |
| 2026-11-06 17:17 | Cursor | @backend/app/worker.py:44-48 explain me is it blocking parallel execution? I got also next explanation from Claude Code: worker.py runs 8 threads by default, they claim segments with SKIP LOCKED, and two documents are translated in parallel. |
| 2026-11-06 17:21 | Cursor | so, shortly I can run one process with 8 threads and can translate 8 documents per time only ? and how many processes I can run there? |
| 2026-11-06 17:24 | Cursor | what happened if I upload 1000 documents? will it wait to keep current rate limit ? and where is rate limit written? |
| 2026-11-06 17:38 | Cursor | how about retry mechanism. Is it possible 2 threads will retry on the same time when they got 429? |
| 2026-11-06 17:40 | Claude Code | Answer. How should the MCP tools behave so one translation never blocks another? → "Async + SSE push". |
| 2026-11-06 17:48 | Cursor | explain me that plan from Claude Code: MCP is async all the way down. translate_document returns a job_id immediately, and wait=true is optional. Job and document state changes are pushed over SSE, backed by Postgres LISTEN/NOTIFY. so is the stream more resource consuming than pooling? |
| 2026-11-06 17:52 | Cursor | it is looks like some message broker with notify helps here, no? I mean instead of postgress stream. But what is the real throughput for postress stream? or it is depends from connections that PgBouncer keeps ? |
| 2026-11-06 18:04 | Cursor | so, when 1 million users opens browser and start uploading files what would happends? |
| 2026-11-06 18:12 | Claude Code | Approved plan rev 2 (hand-check fixes as parallel workstreams, async MCP with SSE push). |
| 2026-11-06 18:27 | Claude Code | /plan read that file .ai/plans/hand-check-06-11-2026/S-job-event-stream.md and check if 1 million users can open browser, upload documents and won't kill DB with many long dtream connections, if yes then plan to use message broker that return back progress of the file translating and then return message as it was done. on the frontend side react app wait for the pub/sub message and change status if browser is open. or fetch progress and status from DB when page is reloaded. |
| 2026-11-06 18:43 | Claude Code | Rejected the plan: why do we need Realtime service (app/realtime/app.py) on that plan? |
| 2026-11-06 18:46 | Claude Code | how that separate service works? is it only connects to the Redis and streams to the frontend browsers? |
| 2026-11-06 18:48 | Claude Code | separate service (then approved plan rev 3: Redis pub/sub and a realtime service) |
| 2026-11-06 18:56 | Claude Code | now also add as additional plan file to the .ai/plans/hand-check-06-11-2026 another point. I need users authorization as history must not be shared between all users. Sync that step with all plans from the folder |
| 2026-11-06 18:57 | Claude Code | Answers. Sign-in → "Email + password". MCP auth → "Personal API token". Translation memory → "Per user". Approved plan rev 4 at 18:59. |
| 2026-11-07 11:30 | Cursor | I have @.ai/plans/ folder and hand-check-06-11-2026 subfolder. There is index file and plans for subtasks that can be run on parallel but some of them must be run one after another. Check index file first and tell me how to effectivelly run those implementation. |
| 2026-11-07 11:38 | Cursor | ok, now follow that plan, create subagents if needed and implement @.ai/plans/hand-check-06-11-2026 |
| 2026-11-08 14:04 | Cursor | I'm opening main page but still not see auth page redirection. where it is ? |
| 2026-11-08 14:11 | Cursor | what is reflow and preserv layout? |
| 2026-11-08 14:13 | Cursor | I see that error message 2 times. "The translation provider asked us to slow down. The job will retry. rate_limited" |
| 2026-11-08 14:19 | Cursor | when I reach translation failed error due to rate limit is it stops and not working and I must try again? with new document ? how it will be possible click retry ? |
| 2026-11-08 14:23 | Cursor | as some segments already translated is it complicated to make button where I can click retry after faild attemts ? |
| 2026-11-08 14:31 | Cursor | ok, build retry button function and button on the frontend |
| 2026-11-08 14:32 | Cursor | how to run application with grafana ? |
| 2026-11-08 14:44 | Cursor | I have next error: frontend build, useEventStream.test.ts Type 'null' is not assignable to type 'string'. |
| 2026-11-08 14:49 | Cursor | fix it |
| 2026-11-08 15:28 | Cursor | I got next error: langfuse Prisma P3005, the database schema is not empty, migrations failed against the translator database. |
| 2026-11-08 15:31 | Cursor | add this step to build steps I can open `docker compose up` and have it up and running without manual commands |
| 2026-11-08 15:34 | Cursor | it is looking on the same transalor db as "db" service from @docker-compose.yml . Why it is planned to be created as separate service? |
| 2026-11-08 15:38 | Cursor | Create the Langfuse database inside the existing Postgres. Implement the plan. |
| 2026-11-08 15:41 | Cursor | what is the default user and password for langfuse? |
| 2026-11-08 15:44 | Cursor | Fix langfuse version. Now it is stale 2.95 but we already have 4.76 |
| 2026-11-09 12:09 | Cursor | why do I need minio? is it for langfuse? |
| 2026-11-09 16:41 | Cursor | I see an error on frontend container: "nginx: [emerg] host not found in upstream "realtime" in /etc/nginx/conf.d/default.conf:11" |
| 2026-11-09 16:42 | Cursor | fix that |
| 2026-11-09 16:57 | Cursor | I have next error on the worker, but on the UI I see "The translation provider asked us to slow down. rate_limited". Handle correctly rate_limited and fund exceded errors |
| 2026-11-09 17:48 | Cursor | commit changes |
| 2026-11-09 17:52 | Cursor | when translation in progress I already can see "compare window" but it is still translating. Add some loader instead of compare window, untill it finish translation. update it dynamically with open window. Also there is progressbar it is showing only start of the operation and not updating during process. |
| 2026-11-09 17:58 | Cursor | when I "save edit" PATCH /api/jobs/…/segments/0 returns Unprocessable Entity. and that is strange to have "Save Edit" button for each segment. It must be one button that saves all changes after I finish review. |
| 2026-11-09 18:00 | Cursor | how to include trace to langfuce? Now I see that service up and running but langfuce is not showing anything. |
| 2026-11-09 18:04 | Cursor | when I open translated page from a history it is showing blank page. I need to reload browser to see translation details. Fix that. it must load translated page info after click |
| 2026-11-09 18:06 | Cursor | add those changes I will only generate token for self but keep instruction at the end. |
| 2026-11-09 18:09 | Cursor | is there any advantages to keep langfuse v4 vs v2? I've seen v2 was one container but it is used postgress instead of redis. but v4 requires clickhouse, redis and minio. |
| 2026-11-09 18:26 | Cursor | Show progress bar when start analyzing document. Now I see just text "Price before translate" and not sure what is happening. then I see page with how many pages needed to be traslate and how many text with images statistic. |
| 2026-11-09 18:35 | Cursor | I added it but see an error "Failed to export spans batch code: 401, reason: Unauthorized" looks like I need to add authorization to langfuse website. Install the Langfuse AI skill from github.com/langfuse/skills and use it to add tracing to this application with Langfuse following best practices. |
| 2026-11-09 18:54 | Cursor | what is "preserve layout" and "reflow" options? I have 2 variants on the app after analyze process ended. |
| 2026-11-09 19:00 | Cursor | what is review and review_agent modes? I see that estimation to translate was "Estimated $0.0115 · Spent $0.5104 · +4339% ". Translation step took around that sum but additionally I see review_agent and review steps |
| 2026-11-09 19:02 | Cursor | where that agent is called in a code? |
| 2026-11-09 19:06 | Cursor | what does it mean flagged segements and when they became flagged? |
| 2026-11-09 19:10 | Cursor | tell me the whole process again when translation happening |
| 2026-11-09 19:23 | Cursor | Put 10 record on a history and upload more 10 when scroll down and then more 10 seps untill loads all if needed |
| 2026-11-09 19:32 | Cursor | check if there clear MCP instruction on @README.md if not fix it and then setup and use MCP to translate some small page from docs/fixtures/generated/book_text_with_figures.pdf. use my user API token. |
| 2026-11-09 21:06 | Cursor | why you deleted vite config? |
| 2026-11-09 21:14 | Cursor | @README.md:90 what does it mean set MCP_WORKSPACE to folder that holds files you will pass? I need to run mcp from the current project and use it on the separate independent service like cursor, claude code etc. Check if that requirement met. |
| 2026-11-09 21:18 | Claude Code | I have MCP_WORKSPACE inside project, but it is looks like files that can be translated must be inside that folder only, so that looks strange. If I connect from the separate tool outside current project and ask to translate file it won't do that? |
| 2026-11-09 21:22 | Claude Code | yes, I would like to use mcp as a service, so /plan to allow upload documents when calling MCP outside docker container (plan approved at 21:28) |
| 2026-11-09 21:23 | Cursor | check current changes and make few commits if needed. there was langfuse configuration and some another changes |
| 2026-11-09 21:35 | Cursor | now check how mcp works and write me spep by step what to do to setup it and upload file |
| 2026-11-09 21:37 | Claude Code | will agent understand to run curl -fsS -F 'file=@/Users/begin29/contract.pdf' 'http://localhost:8080/api/uploads?ticket=…' or I should run it manually? |
| 2026-11-09 21:44 | Claude Code | how to add that mcp to claude code cli using terminal, not changing json? |
| 2026-11-09 21:46 | Cursor | how to add that mcp to cursor cli using terminal, not changing json? |
| 2026-11-09 21:50 | Claude Code | translate file @ua_with_figure.docx to english using stark-translator (sent from a Claude client in ~/Downloads) |
| 2026-11-09 21:53 | Claude Code | give me usecase to use MCP_WORKSPACE. it is looks like redundant as I would like to use MCP as a independent project where client won't know about project structure. they will only have MCP URL and auth token from the user |
| 2026-11-09 21:54 | Claude Code | how ussually e.g. Figma MCP works or Google drive MCP where I can upload documents? |
| 2026-11-09 21:57 | Claude Code | yes, add list_documents and drop path mode over HTTP |
| 2026-11-09 22:16 | Claude Code | I've seen path_not_supported on the frontend, so I can't already upload document via UI ? |
| 2026-11-09 22:18 | Claude Code | translate file @ua_with_figure.docx to english using stark-translator (Downloads client, again) |
| 2026-11-09 22:21 | Claude Code | I'm inside ~Downloads folder from the another claude client, calling claude and telling "translate file @ua_with_figure.docx to english using stark-translator" and when it is calling that MCP tool with path "/Users/begin29/Downloads/ua_with_figure.docx" got [Errno 2] No such file or directory. You just implemented MCP as a separate service but fresh claude is not following upload file and get document id first. Is there any MCP instruction needed? |
| 2026-11-09 22:24 | Claude Code | translate file @ua_with_figure.docx to english using stark-translator (Downloads client, again) |
| 2026-11-09 22:27 | Claude Code | translate file @book_text_with_figures_en.pdf to Ukrainian using stark-translator (Downloads client) |
| 2026-11-09 22:51 | Claude Code | check those requirements [pasted: the assignment's hard requirements and evaluation criteria] and add to @README.md or to the @DECISIONS.md missing information. @PROMPTS.md I will fill for myself with logs. Additionally remove from @DECISIONS.md the fake-provider benchmark numbers. Also for 3 more week add accuracy for complicated documents like documents with images now works not perfect and translates with one letter on the row and another letter on the next row untill full word will be written. For now it is basic MVP to show parallel translating without falling and ability to see estimated price. Also for the next 3 weeks would be good to add better cost estimation, it calculates translation well but additional agent review step is not included and sometimes real budget is +4000% from the estimate but translation itself accurate. Also will improve MCP, as for now it is working on CLI but not in the desktop apps that won't support curl to upload files as it is separate service. |
| 2026-11-09 22:57 | Claude Code | run docker compose up --build from a fresh clone and check if it is up and running |
| 2026-11-09 22:59 | Cursor | commit the changes. split to few if needed |
| 2026-11-09 23:13 | Cursor | commit readme changes |
