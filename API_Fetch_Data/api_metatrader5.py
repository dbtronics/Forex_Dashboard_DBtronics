# pip install MetaTrader5

import MetaTrader5 as mt5
mt5.initialize()

import pandas as pd
from datetime import datetime, timezone, timedelta
import time
import pytz


def fetch_account_info():
    # Read the CSV file into a DataFrame
    df_credentials = pd.read_csv('../../api_credentials.csv') # fetch in root folder of nextcloud

    print(df_credentials)
    
    login_list = []
    alias_list = []
    location_list = []
    balance_list = []
    equity_list = []
    margin_list = []
    margin_free_list = []
    pnl_list = []
    type_list = []
    percentage_difference_balance = [] # (equity-startingBalance)/startingBalance
    percentage_difference_equity = [] # (equity-startingEquity)/startingEquity
    loss_threshold = []
    Server_Time = []
    Local_Time = [] # purpose is to see how current is the data
    
    

    for i in range(df_credentials.shape[0]): # access all rows
        mt5.login(int(df_credentials.iloc[i]["Login"]), df_credentials.iloc[i]["Password"], df_credentials.iloc[i]["Server"]) # login has to be integer otherwise it won't work

        accountInfo = mt5.account_info()
        
        print(accountInfo)

        print("Account: " + str(accountInfo.login))
        print("Balance: " + str(accountInfo.balance))
        print("Equity: " + str(accountInfo.equity))
        print("Profit: " + str(accountInfo.profit))
        print("Free Margin: " + str(accountInfo.margin_free))
        


        # This is needed to extract server time
        # Define the symbol to fetch the last tick time
        symbol = "EURUSD"  # Change this to the symbol you are interested in

        # Request the last tick for the symbol
        last_tick = mt5.symbol_info_tick(symbol)

        # Check if the last tick retrieval was successful
        if last_tick:
            # Retrieve the time of the last tick
            last_tick_time = last_tick.time

            # Convert last tick time from seconds to a datetime object
            last_tick_time_datetime = datetime.fromtimestamp(last_tick_time, timezone.utc)
            print("Server time:", last_tick_time_datetime)
            
            # Define the MST (Mountain Standard Time) timezone offset
            MST = timezone(timedelta(hours=-7))  # MST is UTC-7

            # Convert the server time to MST
            # local_time_mst = last_tick_time_datetime.astimezone(MST)
            # print("Local time (MST):", local_time_mst)
        else:
            print("Failed to retrieve last tick information")
        
        # Set the desired time zone, for example, "US/Mountain" for Mountain Time (MT), which is UTC-7 during standard time.
        # Adjust this to the appropriate time zone as needed.
        tz = pytz.timezone('US/Mountain')
        
        # Get the current time in the specified time zone
        local_time = datetime.now(tz)

        # Format the time as specified
        formatted_time = local_time.strftime('%Y-%m-%d %H:%M:%S%z')

        # Adjust the format to include a colon in the timezone offset
        formatted_time_with_colon = formatted_time[:-2] + ':' + formatted_time[-2:]
        
        local_time_mst = formatted_time_with_colon
        
        print("Local time (MST):", local_time_mst)
        
        print("")

        login_list.append(accountInfo.login)
        alias_list.append(df_credentials.iloc[i]["Alias"])
        location_list.append(df_credentials.iloc[i]["Location"])
        balance_list.append(accountInfo.balance)
        equity_list.append(accountInfo.equity)
        margin_list.append(accountInfo.margin)
        margin_free_list.append(accountInfo.margin_free)
        pnl_list.append(f"{accountInfo.equity - accountInfo.balance:.2f}")
        type_list.append(df_credentials.iloc[i]["Type"])
        Server_Time.append(last_tick_time_datetime)
        Local_Time.append(local_time_mst)
        # TODO: CALCULATION
        if 'demo' not in df_credentials.iloc[i]["Type"]:
            percentage_difference_balance.append(f"{(accountInfo.equity-df_credentials.iloc[i]["Starting_Day_Balance"])/df_credentials.iloc[i]["Starting_Day_Balance"]:.2f}")
            percentage_difference_equity.append(f"{(accountInfo.equity-df_credentials.iloc[i]["Starting_Day_Equity"])/df_credentials.iloc[i]["Starting_Day_Equity"]:.2f}")
            loss_threshold.append(df_credentials.iloc[i]["Today_Loss_Threshold"])
        else:
            percentage_difference_balance.append('N/A')
            percentage_difference_equity.append('N/A')
            loss_threshold.append('N/A')
        
        
    columns = ["ID", "Name", "Location", "Balance", "Equity", "Margin", "Free Margin", "Floating PnL", "Type", "% Difference Balance", "% Difference Equity", "Loss Threshold", "Server Time", "Local Time"]

    df_data = pd.DataFrame(columns=columns)
    # print(type_list)

    df_data["ID"] = login_list
    df_data["Name"] = alias_list
    df_data["Location"] = location_list
    df_data["Balance"] = balance_list
    df_data["Equity"] = equity_list
    df_data["Margin"] = margin_list
    df_data["Free Margin"] = margin_free_list
    df_data["Floating PnL"] = pnl_list
    df_data["Type"] = type_list
    df_data["Server Time"] = Server_Time
    df_data["Local Time"] = Local_Time
    df_data["% Difference Balance"] = percentage_difference_balance
    df_data["% Difference Equity"] = percentage_difference_equity
    df_data["Loss Threshold"] = loss_threshold
    

    df_data.to_csv('api_web.csv', index=False, mode='w')


    df_data

# Loop to fetch information every 10 seconds
count = 0
try:
    while True:
        fetch_account_info()
        count += 1
        print()
        print("INFORMATION FETCHED " + str(count) + " TIMES")
        time.sleep(5)
except KeyboardInterrupt:
    print("Stopped by user")
finally:
    mt5.shutdown()