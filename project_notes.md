Overview

You'll receive three engineering documents. Your job is to design and build a pipeline that extracts structured data from documents like these. In the follow-up session, you'll run your pipeline live on a fourth document you haven't seen and walk us through the results.

Provided materials

Doc	Type	Evaluation
Document 1	P&ID	Answer key provided
Document 2	P&ID	Answer key provided
Document 3	Table-heavy sheet	No answer key. Goal: reproduce every table on the sheet exactly (100% cell-level accuracy)
Answer keys are JSON, one object per document, with a document_id and an answers object of category → list of extracted values (e.g. valves, pressure_indicators, filters, continuation_connections).

What to build

A single pipeline (script, CLI, or notebook) that takes a document as input and produces structured output matching the answer-key schema for P&IDs, and faithful table reproductions for table sheets.
A way to score your output against the answer keys (precision/recall per category is a good start).
A short write-up (1 page max) covering your approach, what you tried and rejected, known failure modes, and what you'd do with more time.
Rules

Any language, tools, libraries, or models are allowed, including LLMs and OCR/vision APIs. 
The pipeline must generalize. Hardcoding answers from the keys or tuning to the specific three documents' quirks will not carry over to the fourth.
Your pipeline must run end-to-end on a new document. You’ll have 10 minutes to adjust any business logic/modify few-shot prompts, but you’ll have to explain why as you do so. 
Bring a working environment. We'll provide the fourth document at the start of the live session. It will look similar to one of the three documents provided. 
Live session (30 minutes)

You run your pipeline on the fourth document.
We review the output together. Expect questions on where it's right, where it's wrong, and why.
Discussion of design tradeoffs and how you'd improve it.
What we're evaluating

Accuracy on the answer-keyed documents and table reproduction
Robustness on the unseen document
Engineering judgment: evaluation, error analysis, tradeoffs
Clarity of communication
Ability to work with technical documents in a field you’re unfamiliar with. If you don’t know how to recognize something like 'pressure_control_valves (U-bend_pipe)’, take your best guess at it. In your write-up, treat us like we’re the SMEs and ask us the questions in the live interview about technical material you didn’t understand. We expect you to ask 1-2 questions while you’re few-shooting the live example about what different elements mean. 
