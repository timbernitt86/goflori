from app import create_app
from app.cli import register_cli

app = create_app()
register_cli(app)

if __name__ == "__main__":
    app.run(debug=True)
