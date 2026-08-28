import __init__

# Test parsing
print("=== Parsing ===")
for text in [
    "When building with make, use python build.py instead of make.",
    "When dotnet command is not recognized, use full path instead of dotnet CLI.",
    "When calling create_coding_task, always include both 'prompt' and 'source' fields.",
]:
    r = __init__._parse_prompt_note_rule(text)
    print(f"  {text[:60]}...")
    print(f"    -> {r}")

# Test binary extraction
print("\n=== Binary extraction ===")
for cmd in ["make build", "cmake --build build", "git commit -m make", "dotnet build"]:
    b = __init__._extract_binary(cmd)
    m = __init__._binary_matches(b, "make") if b else False
    print(f"  {cmd:30s} binary={b or 'None':10s} matches_make={m}")

# Test full hook
print("\n=== Hook with make rule ===")
rule = __init__._parse_prompt_note_rule(
    "When building with make, use python build.py instead of make."
)
__init__._BLOCK_RULES = [rule]
for cmd in ["make build", "cmake --build build", "git commit -m fix", "make all", "python build.py"]:
    r = __init__._on_pre_tool_call("terminal", {"command": cmd})
    print(f"  {cmd:30s} -> {'BLOCK' if r else '-'}  {r.get('message','') if r else ''}")
