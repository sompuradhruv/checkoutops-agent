"""Runbook search: retrieval over the team's markdown runbooks.

Runbooks are split into sections (one chunk per '## ' heading) and ranked with
BM25. Keyword ranking suits this corpus: error codes, metric names and service
names need exact matches. With a larger corpus you would add embeddings and use
hybrid search (for example Azure AI Search).
"""
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

RUNBOOK_DIR=Path(__file__).resolve().parents[2]/"runbooks"
STOPWORDS=set("a an the and or of to in on for is are be it this that with as at by from not no do if than then".split())


def tokenize(text):
    return [w for w in re.findall(r"[a-z0-9_\-]+", text.lower()) if w not in STOPWORDS]


@lru_cache(maxsize=1)
def loadChunks():
    chunks=[]
    for path in sorted(RUNBOOK_DIR.glob("*.md")):
        text=path.read_text()
        titleMatch=re.match(r"#\s+(.+)", text)
        title=titleMatch.group(1).strip() if titleMatch else path.stem
        parts=re.split(r"\n(?=## )", text)
        intro=re.sub(r"^#\s+.+\n?", "", parts[0]).strip()
        sections=[("Overview", intro)] if intro else []
        for part in parts[1:]:
            headingMatch=re.match(r"##\s+(.+)", part)
            sections.append((headingMatch.group(1).strip(), part[headingMatch.end():].strip()))
        for section, body in sections:
            chunks.append({"runbook": path.name, "title": title, "section": section, "text": body,
                           "tokens": tokenize(title+" "+section+" "+body)})
    return chunks


def bm25Scores(queryTokens, chunks, k1=1.4, b=0.75):
    n=len(chunks)
    avgLen=sum(len(c["tokens"]) for c in chunks)/max(1, n)
    df=Counter()
    for c in chunks:
        df.update(set(c["tokens"]))
    scores=[]
    for c in chunks:
        tf=Counter(c["tokens"])
        score=0.0
        for q in queryTokens:
            if q not in tf:
                continue
            idf=math.log(1+(n-df[q]+0.5)/(df[q]+0.5))
            score+=idf*tf[q]*(k1+1)/(tf[q]+k1*(1-b+b*len(c["tokens"])/avgLen))
        scores.append(score)
    return scores


def searchRunbooks(query, limit=3):
    chunks=loadChunks()
    queryTokens=tokenize(query)
    if not queryTokens:
        return {"results": [], "hint": "Query had no searchable words."}
    scores=bm25Scores(queryTokens, chunks)
    ranked=sorted(zip(scores, chunks), key=lambda x: -x[0])
    results=[{"runbook": c["runbook"], "title": c["title"], "section": c["section"],
              "score": round(s, 2), "text": c["text"][:900]} for s, c in ranked[:limit] if s>0]
    return {"query": query, "results": results} if results else {"query": query, "results": [], "hint": "No matching runbook sections."}
