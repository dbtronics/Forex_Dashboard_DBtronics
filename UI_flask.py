from flask import Flask, render_template, redirect, url_for, request
import pandas as pd

def read_csv_with_pandas(filename):
    df = pd.read_csv(filename)
    
    # Ensure the 'Type' column values are in lowercase
    if 'Type' in df.columns:
        df['Type'] = df['Type'].str.lower()
    
    headers = tuple(df.columns)
    entries = tuple(tuple(row) for row in df.values.tolist())
    return (headers, entries)

app = Flask(__name__)


@app.route("/")
def home():
# Example usage:
    filename = 'api_web.csv'  # Replace 'data.csv' with your CSV file name
    headings, data = read_csv_with_pandas(filename)
    return render_template('index2.html', headings=headings,data=data)


if __name__ == "__main__":
    app.run(debug=True)
