from ib_insync import Order, LimitOrder, StopOrder
import math

def build_two_tranche_brackets(parent_order_id, action, total_quantity, entry_price, stop_price, target_1r, target_2r, market):
    exit_action = 'SELL' if action == 'BUY' else 'BUY'
    
    # Calculate tranches (Leg 1 gets the odd share if total_quantity is uneven)
    q2 = math.floor(total_quantity / 2)
    q1 = total_quantity - q2
    
    # ATR Calculation for the Trailing Stop (Distance from Entry to Stop Loss)
    atr_trail_distance = round(abs(entry_price - stop_price) * 2, 2)
    
    # ---------------------------
    # PARENT ENTRY ORDER (MTL)
    # ---------------------------
    parent = Order(
        orderId=parent_order_id,
        action=action,
        orderType='MTL',
        totalQuantity=total_quantity,
        tif='DAY',
        outsideRth=False,
        transmit=False
    )
    
    # ---------------------------
    # LEG 1: 1.0R Target + Stop
    # ---------------------------
    tp1 = LimitOrder(exit_action, q1, target_1r)
    tp1.parentId = parent_order_id
    tp1.tif = 'DAY'
    tp1.outsideRth = False
    tp1.transmit = False
    
    sl1 = StopOrder(exit_action, q1, stop_price)
    sl1.parentId = parent_order_id
    sl1.tif = 'DAY'
    sl1.outsideRth = False
    sl1.transmit = False
    
    # ---------------------------
    # LEG 2: Uncapped Runner (Trailing Stop)
    # ---------------------------
    # No Target Limit Order for Leg 2. It runs until stopped out or MOC.
    
    sl2_trail = Order(
        action=exit_action,
        orderType='TRAIL',
        totalQuantity=q2,
        auxPrice=atr_trail_distance,
        trailStopPrice=stop_price,
        parentId=parent_order_id,
        tif='DAY',
        outsideRth=False,
        transmit=True  # Final child order must transmit the entire bracket
    )
    
    # Return Parent and the 3 children (TP1, SL1, SL2_TRAIL)
    return parent, [tp1, sl1, sl2_trail]
