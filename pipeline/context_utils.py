import re
import json

def clean_repetitive_words(text: str, max_repeats: int = 3) -> str:
    """
    Cleans consecutive repeated words and multi-word phrases to prevent LLM loops.
    E.g. "Randheer. Randheer. Randheer. Randheer." -> "Randheer. Randheer. Randheer."
    E.g. "This is a sentence. This is a sentence. This is a sentence. This is a sentence."
         -> "This is a sentence. This is a sentence. This is a sentence."
    """
    if not text or not isinstance(text, str):
        return text

    # 1. Phrase-level cleaning: Match any phrase (12 to 1000 chars) repeating 3+ times consecutively
    text = re.sub(r'(.{5,1000}?)\1{3,}', r'\1\1\1', text, flags=re.DOTALL)

    # 2. Word-level cleaning: Match individual words repeating consecutively
    tokens = re.split(r'(\W+)', text)
    
    cleaned_tokens = []
    last_word_normalized = None
    consecutive_count = 0
    
    for token in tokens:
        # Check if the token contains alphanumeric characters (it is a word)
        if re.search(r'\w', token):
            word_normalized = token.strip().lower()
            if word_normalized == last_word_normalized:
                consecutive_count += 1
            else:
                last_word_normalized = word_normalized
                consecutive_count = 1
            
            if consecutive_count <= max_repeats:
                cleaned_tokens.append(token)
        else:
            # Punctuation or whitespace token
            # Only append if we haven't truncated the preceding word
            if consecutive_count <= max_repeats:
                cleaned_tokens.append(token)
                
    return "".join(cleaned_tokens).strip()


def repair_and_parse_json(raw_text: str) -> dict:
    """
    Collapses loops in raw JSON string, fixes unclosed quotes/braces from truncation,
    and returns parsed dictionary.
    """
    raw_text = raw_text.strip()
    
    # Clean loops in the raw string first so we don't parse megabytes of repetitions
    raw_text = clean_repetitive_words(raw_text)
    
    try:
        return json.loads(raw_text, strict=False)
    except json.JSONDecodeError:
        # Attempt to repair unclosed quote
        quotes_count = len(re.findall(r'(?<!\\)"', raw_text))
        if quotes_count % 2 != 0:
            raw_text += '"'
            
        # Balance curly braces
        open_braces = raw_text.count('{')
        close_braces = raw_text.count('}')
        if open_braces > close_braces:
            raw_text += '}' * (open_braces - close_braces)
            
        return json.loads(raw_text, strict=False)

