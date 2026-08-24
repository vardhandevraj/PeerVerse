with open("app.py", "r") as f:
    code = f.read()
code = code.replace("VALUES (%s, %s)\n            ON CONFLICT DO NOTHING\"", "VALUES (%s, %s) ON CONFLICT DO NOTHING\"")
code = code.replace("VALUES (%s, %s, 'pending')\n            ON CONFLICT DO NOTHING\"", "VALUES (%s, %s, 'pending') ON CONFLICT DO NOTHING\"")
with open("app.py", "w") as f:
    f.write(code)
