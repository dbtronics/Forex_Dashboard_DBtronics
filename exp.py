import pandas as pd

def read_csv_with_pandas(filename):
    df = pd.read_csv(filename)
    headers = tuple(df.columns)
    entries = tuple(tuple(row) for row in df.values.tolist())
    return (headers, entries)

# Example usage:
filename = 'API_Fetch_Data/api_web.csv'  # Replace 'data.csv' with your CSV file name
headers, entries = read_csv_with_pandas(filename)
print("Headers:", headers)
print("Entries:", entries)