import os
import random
import string
import discord
import asyncpg
import aiohttp
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

db_pool = None


def generate_code():
    characters = string.ascii_uppercase + string.digits
    random_part = "".join(random.choices(characters, k=5))
    return f"ACE-{random_part}"


async def setup_database():
    global db_pool

    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                discord_id BIGINT PRIMARY KEY,
                referral_code VARCHAR(20) UNIQUE NOT NULL,
                referral_count INTEGER DEFAULT 0,
                sales_generated NUMERIC(10, 2) DEFAULT 0,
                reward_balance NUMERIC(10, 2) DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

    print("Database connected and referral table ready!")


async def get_referral(discord_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM referrals WHERE discord_id = $1",
            discord_id
        )


async def create_referral(discord_id):
    async with db_pool.acquire() as conn:

        existing = await conn.fetchrow(
            "SELECT * FROM referrals WHERE discord_id = $1",
            discord_id
        )

        if existing:
            return existing

        while True:
            code = generate_code()

            code_exists = await conn.fetchval(
                "SELECT 1 FROM referrals WHERE referral_code = $1",
                code
            )

            if not code_exists:
                break

        return await conn.fetchrow(
            """
            INSERT INTO referrals (
                discord_id,
                referral_code
            )
            VALUES ($1, $2)
            RETURNING *;
            """,
            discord_id,
            code
        )


class ReferralView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Generate Referral Code",
        style=discord.ButtonStyle.green,
        emoji="🎁",
        custom_id="ace_generate_referral"
    )
    async def generate_referral(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:
            referral = await create_referral(interaction.user.id)

            code = referral["referral_code"]

            embed = discord.Embed(
                title="🎉 Your Ace Referral Code",
                description=(
                    f"Your personal referral code is:\n\n"
                    f"## `{code}`\n\n"
                    "Share this code with someone purchasing from "
                    "**Ace Services**.\n\n"
                    "🏷️ They receive a discount when using your code.\n"
                    "💰 Completed purchases count toward your referrals.\n\n"
                    "**Your code is permanently linked to your account.**"
                )
            )

            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )

        except Exception as error:
            print(f"Referral button error: {error}")

            await interaction.response.send_message(
                "❌ Something went wrong while generating your referral code.",
                ephemeral=True
            )


@bot.command()
async def referrals(ctx):

    referral = await get_referral(ctx.author.id)

    if not referral:
        await ctx.reply(
            "You haven't generated a referral code yet!"
        )
        return

    embed = discord.Embed(
        title="💎 Your Ace Referral Stats"
    )

    embed.add_field(
        name="🎟️ Referral Code",
        value=f"`{referral['referral_code']}`",
        inline=False
    )

    embed.add_field(
        name="👥 Successful Referrals",
        value=str(referral["referral_count"])
    )

    embed.add_field(
        name="💰 Sales Generated",
        value=f"${float(referral['sales_generated']):.2f}"
    )

    embed.add_field(
        name="🎁 Reward Balance",
        value=f"${float(referral['reward_balance']):.2f}"
    )

    await ctx.reply(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def referralpanel(ctx):

    embed = discord.Embed(
        title="💎 ACE SERVICES REFERRAL PROGRAM",
        description=(
            "Invite people to **Ace Services** and earn rewards!\n\n"
            "🎁 Click below to generate your personal referral code.\n\n"
            "🏷️ Your referrals receive a discount on their purchase.\n"
            "💰 Completed purchases count toward your referral rewards.\n"
            "🔒 Every member receives one permanent referral code.\n\n"
            "**Click below to get started!**"
        )
    )

    await ctx.send(
        embed=embed,
        view=ReferralView()
    )


@bot.event
async def on_ready():

    print(f"Logged in as {bot.user}")
    print("Ace Referral Bot is online!")


async def main():

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")

    await setup_database()

    bot.add_view(ReferralView())

    await bot.start(TOKEN)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
