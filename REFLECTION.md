# Reflection

## Design decision

One of my main design decisions was to make the tools the single source of truth for money-related information. I wanted the agent to use memory for things such as who the customer is and their preferences, but not rely on memory for prices, order totals, refunds, or other figures that can change. Memory is useful for personalisation, but financial information stored there can become outdated. I also strip dollar amounts before saving information to memory.

I also learned that it is not enough to tell the model in the prompt to pass the correct refund amount, so I validate it in the refund Lambda as well. The Lambda rejects a zero refund or an amount greater than the order total. The prompt guides the model, while the code provides a stronger guarantee around the actual refund amount.

## Challenge

The biggest challenge was Test 5. My first attempt gave the wrong answer of $95. I initially thought stale memory was responsible, so I stopped saving dollar amounts in memory and added a stronger prompt instruction. The result was still wrong.

I then cleared the memory, which fixed Test 4 because the agent could correctly recall Jane again, but Test 5 still failed. That showed me that memory was not actually causing the discount problem.

After investigating further, I found that the tool was returning $99, while the agent was redoing the calculation and arriving at the wrong amount. The correct calculation was based on $110, but the model was taking 10% of $150 instead. I documented the order of operations and instructed the agent to report the tool’s figures exactly. This fixed the test.

## Production consideration

Before using this system in production, I would address several gaps. First, the Gateway currently has no authentication, so I would add an authorizer before exposing the tools publicly. Second, the refund Lambda should not maintain its own copy of order totals. In production, it should retrieve authoritative order information from a shared database.

I would also define a clearer memory policy covering what financial information and point balances can be stored, how long that information is retained, and how it is deleted. Finally, I would add monitoring that records tool calls and compares the agent’s final response with the tool result. Test 5 showed why this matters: the tool can return $99 while the agent still tells the customer $95.
