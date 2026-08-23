with open("app.py", "r") as f:
    code = f.read()
code = code.replace("socketio.run(app, debug=True, port=int(os.environ.get(\"PORT\", \"5001\")))", "socketio.run(app, debug=True, port=int(os.environ.get(\"PORT\", \"5001\")), allow_unsafe_werkzeug=True)")
code = code.replace("socketio.run(app, debug=True)", "socketio.run(app, debug=True, port=int(os.environ.get(\"PORT\", \"5001\")), allow_unsafe_werkzeug=True)")
with open("app.py", "w") as f:
    f.write(code)
