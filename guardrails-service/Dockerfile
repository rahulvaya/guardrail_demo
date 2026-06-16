FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
# Point NLTK at a system-wide directory so the non-root app user can read data
# pre-downloaded below. Writing to /app is blocked for the app user at runtime.
ENV NLTK_DATA=/usr/local/share/nltk_data

RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt \
    # Pre-download NLTK corpora used by the NLP evaluators (BLEU, GLEU, METEOR).
    # Downloaded as root during build so the app user only needs read access.
    && python -m nltk.downloader -d /usr/local/share/nltk_data \
        punkt punkt_tab wordnet perluniprops stopwords averaged_perceptron_tagger \
    && chmod -R a+rX /usr/local/share/nltk_data

# Service code + bundled default policies.
COPY app /app/app

USER app

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
