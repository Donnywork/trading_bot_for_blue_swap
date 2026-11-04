

* uniswap quotev2 contract addr: 0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3
  (uniswap docs: https://developers.uniswap.org/docs/protocols/v3/deployments/v3-ethereum-deployments)
  
  * check abi: https://sepolia.etherscan.io/address/0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3#code

* swap routerv2 addr: 0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E
  
  * check abi at https://sepolia.etherscan.io/address/0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E#code

* pool contract addr: 0x6a8BCbC98ff2485582049e67299fb16ad900728B
  
  * check abi: https://sepolia.etherscan.io/address/0x6a8BCbC98ff2485582049e67299fb16ad900728B#code

- token:
  
  - f564: 0xfd840838F598d7abB153a8bc93f4a2FF38D2D5B8
  - feth: 0x1967a5A5f12Cbd95f12847067C2c00F044D3EF11

* wallet addr: 0x9Cb82eDACbdA4bDB053a3E7D5EccD6798aDDc731

* procedure:
  
  * pool read price
  
  * decide on strategy
  
  * quotev2 simulate transaction
  
  * approve spending cap(token addr, spender, amount)
  
  * swap by calling swap router exactinputsingle
  
  * 
