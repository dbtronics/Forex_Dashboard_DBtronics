Input: api_credentials.csv
Description: This has all the credentials and has been saved in root folder of NextCloud to protect the crednetials to access all MT5 files while this repo is public.

Output: api_web.csv
Description: Fetches all the data from MT5 using api_credentials.csv and saves the result in api_web.csv for front-end dashboard using flask server to handle HTTP request. The front-end will be hosted at forex.dbtronics.org