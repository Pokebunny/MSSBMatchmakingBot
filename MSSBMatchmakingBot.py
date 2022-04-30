# file: MSSBMatchmakingBot.py
# author: Nick Taber / Pokebunny
# date: 4/30/22

import os
import time

from discord.ext import commands, tasks
from dotenv import load_dotenv

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# load .env file which has discord token
load_dotenv()
TOKEN = os.getenv('MMBOT_TOKEN')

# initialize the bot commands with the associated prefix
bot = commands.Bot(command_prefix='%')

# use creds to create a client to interact with the Google Drive API
scope = ['https://spreadsheets.google.com/feeds',
         'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('client_secret.json', scope)
client = gspread.authorize(creds)

# Access spreadsheet and store data
stars_off_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("STARS-OFF")
stars_on_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("STARS-ON")
off_log_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("Logs-OFF")
on_log_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("Logs-ON")
# Create a list of all player ratings (to be used for defining percentile search ranges)
off_rating_list = list(map(int, stars_off_sheet.col_values(5)[1:]))
on_rating_list = list(map(int, stars_on_sheet.col_values(5)[1:]))

# Constant for starting percentile range for matchmaking search
PERCENTILE_RANGE = 0.10
# Constant to tell the bot where to post matchmaking updates
MM_CHANNEL_ID = 968987148357349399
# The matchmaking queue
queue = {}


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    # Start timed tasks
    refresh_queue.start()
    refresh_api_data.start()


# Command for a player to enter the matchmaking queue
# If they are in the queue already, it will refresh their presence in the queue
# You can also move from one queue to another with this
@bot.command(name="queue", aliases=["q"], help="Enter queue")
async def enter_queue(ctx, game_type="ranked"):
    await ctx.message.delete()
    if game_type.lower() not in ["ranked", "unranked", "stars"]:
        await ctx.send("Invalid game type")
        return False
    player_rating = 1400
    player_id = str(ctx.author.id)
    player_name = ctx.author.name
    if game_type == "stars":
        # TODO: Avoid accessing the API every time someone queues
        matches = on_log_sheet.findall(player_id)
        if matches:
            player_rating = round(float(on_log_sheet.cell(matches[-1].row, matches[-1].col + 3).value))
    else:
        # TODO: Avoid accessing the API every time someone queues
        matches = off_log_sheet.findall(player_id)
        if matches:
            player_rating = round(float(off_log_sheet.cell(matches[-1].row, matches[-1].col + 3).value))

    # put player in queue
    queue[player_id] = {"Name": player_name, "Rating": player_rating, "Time": time.time(), "Game Type": game_type}

    # calculate search range
    min_rating, max_rating = calc_search_range(player_rating, game_type.lower(), PERCENTILE_RANGE)

    # check for match
    await check_for_match(player_id, min_rating, max_rating, 0)

    await post_queue_status(ctx.channel.id)


# Command for a player to remove themselves from the queue
# If they aren't in the queue, it will just post a message with the queue status
@bot.command(name="dequeue", aliases=["dq"], help="Exit queue")
async def exit_queue(ctx):
    await ctx.message.delete()
    if str(ctx.author.id) in queue:
        del queue[str(ctx.author.id)]
    await post_queue_status(ctx.channel.id)


# refresh to see if a match can now be created with players waiting in the queue
@tasks.loop(seconds=30)
async def refresh_queue():
    for player in queue:
        time_in_queue = time.time() - queue[player]["Time"]
        if time_in_queue > 120:
            new_range = PERCENTILE_RANGE * time_in_queue / 120
            min_rating, max_rating = calc_search_range(queue[player]["Rating"], queue[player]["Game Type"], new_range)
            if await check_for_match(player, min_rating, max_rating, 120):
                post_queue_status(MM_CHANNEL_ID)
                break


# update spreadsheet API data once per minute
@tasks.loop(minutes=1)
async def refresh_api_data():
    global stars_off_sheet, stars_on_sheet, off_log_sheet, on_log_sheet, off_rating_list, on_rating_list
    stars_off_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("STARS-OFF")
    stars_on_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("STARS-ON")
    off_log_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("Logs-OFF")
    on_log_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("Logs-ON")
    off_rating_list = list(map(int, stars_off_sheet.col_values(5)[1:]))
    on_rating_list = list(map(int, stars_on_sheet.col_values(5)[1:]))


# Send a message with the current queue status to the designated channel
async def post_queue_status(channel_id):
    channel = bot.get_channel(channel_id)
    ranked_q = unranked_q = stars_q = 0
    for user in queue:
        if queue[user]["Game Type"] == "ranked":
            ranked_q += 1
        if queue[user]["Game Type"] == "unranked":
            unranked_q += 1
        if queue[user]["Game Type"] == "stars":
            stars_q += 1
    print(queue)
    await channel.send("There are " + str(len(queue)) + " users in the matchmaking queue (" + str(ranked_q) + " ranked, " + str(unranked_q) + " unranked, " + str(stars_q) + " stars-on)")


# params: player's rating and what percentile you want your search range to cover
# return: min and max rating the player can match against
def calc_search_range(rating, game_type, percentile):
    if game_type == "stars":
        rating_list_copy = on_rating_list.copy()
    else:
        rating_list_copy = off_rating_list.copy()
    rating_list_copy.append(rating)
    pct_list = sorted(rating_list_copy, reverse=True)
    max_index = round(pct_list.index(rating) - (len(pct_list) * percentile))
    min_index = round(pct_list.index(rating) + (len(pct_list) * percentile))
    if max_index < 0:
        max_index = 0
    if min_index >= len(pct_list):
        min_index = len(pct_list) - 1

    max_rating = pct_list[max_index]
    min_rating = pct_list[min_index]

    return min_rating, max_rating


# Checks if there is an available match for a user.
# Uses their user_id, search range (min-max ratings), and the min time an opponent must be searching to be matched.
async def check_for_match(user_id, min_rating, max_rating, min_time):
    print("Rating:", queue[user_id]["Rating"], "Time:", round(time.time() - queue[user_id]["Time"]), "Rating Range", min_rating, max_rating)
    channel = bot.get_channel(MM_CHANNEL_ID)
    if len(queue) >= 2:
        best_match = False
        for player in queue:
            if max_rating >= queue[player]["Rating"] >= min_rating and \
                    player != user_id and time.time() - queue[player]["Time"] > min_time and \
                    queue[player]["Game Type"] == queue[user_id]["Game Type"]:
                if not best_match or abs(queue[best_match]["Rating"] - queue[user_id]["Rating"]) > abs(queue[player]["Rating"] - queue[user_id]["Rating"]):
                    best_match = player

        if best_match:
            await channel.send("We have a match! <@" + user_id + "> vs <@" + best_match + ">")
            del queue[best_match]
            del queue[user_id]
            return True
        else:
            return False


# run the bot
bot.run(TOKEN)
