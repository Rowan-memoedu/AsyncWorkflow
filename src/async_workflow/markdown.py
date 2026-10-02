from markdown_it import MarkdownIt


def parse_outline(markdown: str) -> dict:
    tokens = MarkdownIt("commonmark", {"html": False}).parse(markdown.strip())
    if len(tokens) < 3 or tokens[0].type != "heading_open" or tokens[0].tag != "h1":
        raise ValueError("outline must start with one H1")
    root = {"text": tokens[1].content, "children": []}
    stack, pending = [root], False
    for tok in tokens[3:]:
        if tok.type == "list_item_open":
            node = {"text": "", "children": []}
            stack[-1]["children"].append(node)
            stack.append(node)
        elif tok.type == "list_item_close":
            if not stack[-1]["text"].strip():
                raise ValueError("empty bullet")
            stack.pop()
        elif tok.type == "inline":
            if len(stack) == 1:
                raise ValueError("body must consist of bullets")
            if stack[-1]["text"]:
                stack[-1]["text"] += "\n"
            stack[-1]["text"] += tok.content
        elif tok.type not in {"bullet_list_open", "bullet_list_close", "paragraph_open", "paragraph_close"}:
            raise ValueError("unexpected outline token: " + tok.type)
    if len(stack) != 1 or not root["text"].strip():
        raise ValueError("invalid outline tree")
    return root
