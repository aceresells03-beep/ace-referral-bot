import os
import random
import string
import asyncio

import discord
import asyncpg
import aiohttp
from discord.ext import commands


# =========================
# ENVIRONMENT VARIABLES
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY")
SELLAUTH_SHOP_ID = os.getenv("SELLAUTH_SHOP_ID")


# =========================
# DISCORD SETUP
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

db_pool = None


# =========================
# REFERRAL CODE GENERATOR
# =========================

def generate_code():
    characters = string.ascii_uppercase + string.digits
    random_part = "".join(
        random.choices(characters, k=5)
    )

    return f"ACE-{random_part}"


# =========================
# SELLAUTH COUPONS
# =========================

async def create_sellauth_coupon(code):

    url = (
        f"https://api.sellauth.com/v1/shops/"
        f"{SELLAUTH_SHOP_ID}/coupons"
    )

    headers = {
        "Authorization": f"Bearer {SELLAUTH_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "code": code,
        "global": True,
        "discount": 10,
        "type": "percentage",
        "max_uses_per_customer": 1,
        "min_invoice_price": 0,
        "disable_if_volume_discount": False,
        "disable_if_quantity_deal": False,
        "disable_if_bundle_offer": False,
        "disable_if_gateway_discount": False,
        "disable_if_affiliate_discount": True,
        "uses_count_mode": "paid",
        "applies_to_renewals": False
    }

    async with aiohttp.ClientSession() as session:

        async with session.post(
            url,
            json=payload,
            headers=headers
        ) as response:

            response_text = await response.text()

            if response.status not in (200, 201):

                print(
                    "SellAuth coupon creation failed "
                    f"({response.status}): "
                    f"{response_text}"
                )

                return False

            print(
                f"SellAuth coupon created: {code}"
            )

            return True


# =========================
# DATABASE
# =========================

async def setup_database():

    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL
    )

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                discord_id BIGINT PRIMARY KEY,
                referral_code VARCHAR(20)
                    UNIQUE NOT NULL,
                referral_count INTEGER
                    DEFAULT 0,
                sales_generated NUMERIC(10, 2)
                    DEFAULT 0,
                reward_balance NUMERIC(10, 2)
                    DEFAULT 0,
                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    print(
        "Database connected and "
        "referral table ready!"
    )


async def get_referral(discord_id):

    async with db_pool.acquire() as conn:

        return await conn.fetchrow(
            """
            SELECT *
            FROM referrals
            WHERE discord_id = $1
            """,
            discord_id
        )


async def create_referral(discord_id):

    async with db_pool.acquire() as conn:

        existing = await conn.fetchrow(
            """
            SELECT *
            FROM referrals
            WHERE discord_id = $1
            """,
            discord_id
        )

        # User already has a referral code
        if existing:
            return existing

        # Generate a new unique code
        while True:

            code = generate_code()

            code_exists = await conn.fetchval(
                """
                SELECT 1
                FROM referrals
                WHERE referral_code = $1
                """,
                code
            )

            if code_exists:
                continue

            # Create matching 10% SellAuth coupon
            coupon_created = (
                await create_sellauth_coupon(code)
            )

            if coupon_created:
                break

        # Only save the referral after
        # SellAuth successfully creates it
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


# =========================
# REFERRAL BUTTON
# =========================

class ReferralView(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

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

            referral = await create_referral(
                interaction.user.id
            )

            code = referral[
                "referral_code"
            ]

            embed = discord.Embed(
                title=(
                    "🎉 Your Ace Referral Code"
                ),
                description=(
                    "Your personal referral "
                    "code is:\n\n"
                    f"## `{code}`\n\n"
                    "Share this code with "
                    "someone purchasing from "
                    "**Ace Services**.\n\n"
                    "🏷️ They receive **10% "
                    "off** when using your "
                    "code.\n"
                    "💰 Completed purchases "
                    "count toward your "
                    "referrals.\n\n"
                    "**Your code is permanently "
                    "linked to your account.**"
                )
            )

            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )

        except Exception as error:

            print(
                f"Referral button error: "
                f"{error}"
            )

            if not interaction.response.is_done():

                await interaction.response.send_message(
                    "❌ Something went wrong "
                    "while generating your "
                    "referral code.",
                    ephemeral=True
                )


# =========================
# REFERRAL STATS COMMAND
# =========================

@bot.command()
async def referrals(ctx):

    referral = await get_referral(
        ctx.author.id
    )

    if not referral:

        await ctx.reply(
            "You haven't generated a "
            "referral code yet!"
        )

        return

    embed = discord.Embed(
        title="💎 Your Ace Referral Stats"
    )

    embed.add_field(
        name="🎟️ Referral Code",
        value=(
            f"`{referral['referral_code']}`"
        ),
        inline=False
    )

    embed.add_field(
        name="👥 Successful Referrals",
        value=str(
            referral["referral_count"]
        )
    )

    embed.add_field(
        name="💰 Sales Generated",
        value=(
            f"${float(referral['sales_generated']):.2f}"
        )
    )

    embed.add_field(
        name="🎁 Reward Balance",
        value=(
            f"${float(referral['reward_balance']):.2f}"
        )
    )

    await ctx.reply(
        embed=embed
    )


# =========================
# ADMIN REFERRAL PANEL
# =========================

@bot.command()
@commands.has_permissions(
    administrator=True
)
async def referralpanel(ctx):

    embed = discord.Embed(
        title=(
            "💎 ACE SERVICES "
            "REFERRAL PROGRAM"
        ),
        description=(
            "Invite people to "
            "**Ace Services** and "
            "earn rewards!\n\n"
            "🎁 Click below to generate "
            "your personal referral "
            "code.\n\n"
            "🏷️ Your referrals receive "
            "**10% off** their purchase.\n"
            "💰 Completed purchases "
            "count toward your referral "
            "rewards.\n"
            "🔒 Every member receives "
            "one permanent referral "
            "code.\n\n"
            "**Click below to get "
            "started!**"
        )
    )

    await ctx.send(
        embed=embed,
        view=ReferralView()
    )


# =========================
# BOT EVENTS
# =========================

@bot.event
async def on_ready():

    print(
        f"Logged in as {bot.user}"
    )

    print(
        "Ace Referral Bot is online!"
    )


# =========================
# START BOT
# =========================

async def main():

    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is missing."
        )

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is missing."
        )

    if not SELLAUTH_API_KEY:
        raise RuntimeError(
            "SELLAUTH_API_KEY is missing."
        )

    if not SELLAUTH_SHOP_ID:
        raise RuntimeError(
            "SELLAUTH_SHOP_ID is missing."
        )

    await setup_database()

    bot.add_view(
        ReferralView()
    )

    await bot.start(
        TOKEN
    )


if __name__ == "__main__":

    asyncio.run(
        main()
    )
